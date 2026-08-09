"""API 金鑰的本機加密儲存。

設計取捨（回應「或者有更好的方法」）：
本機桌面程式若用「程式內寫死的金鑰」去加密金鑰檔，等於只是混淆——任何人拿到
原始碼＋檔案都能解。真正可靠且不必每次輸入主密碼的做法，是用作業系統提供、
綁定使用者帳號的加密：

    Windows DPAPI（Data Protection API，透過 ctypes 呼叫 Crypt32.dll）
      ✓ 加密金鑰由 OS 依「目前 Windows 使用者」管理，程式碼裡沒有任何祕密
      ✓ 換台電腦、換 Windows 使用者都無法解密
      ✓ 不必每次啟動輸入主密碼
      ✓ 不需第三方套件（ctypes 為標準庫）

因此本模組在 Windows 上一律走 DPAPI；非 Windows 平台沒有等價的零密碼方案，
退回 base64 混淆（**非真正加密**，僅避免肉眼直接讀到，並在檔頭標記）。

金鑰實際存於 ``aa_api_keys.dat``（已列入 .gitignore），不會進 git。

**多供應商格式**：檔內以 ``{"providers": {供應商: [金鑰...]}}`` 分別記住各家
（gemini／openai／claude／deepseek／custom）的金鑰，切換供應商時彼此不覆蓋。
舊版單一池格式 ``{"keys": [...]}`` 於載入時自動視為屬於 ``gemini``（API 模式先前僅
支援 Gemini），下次寫入即升級為新格式。``load_keys``／``save_keys`` 的 ``provider``
預設 ``gemini``，維持既有呼叫端相容。
"""
from __future__ import annotations

import base64
import json
import os
import sys

KEYS_FILENAME = "aa_api_keys.dat"

_MAGIC_DPAPI = b"DPAPI1\n"
_MAGIC_B64 = b"B64\n"
_IS_WIN = sys.platform == "win32"


# ── Windows DPAPI（ctypes） ──

def _dpapi(raw: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = DATA_BLOB(len(raw),
                        ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if encrypt
          else ctypes.windll.crypt32.CryptUnprotectData)
    # 旗標 0；CRYPTPROTECT_UI_FORBIDDEN 不需要（純背景操作）
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI " + ("加密" if encrypt else "解密") + "失敗")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _keys_path(base_dir: str) -> str:
    return os.path.join(base_dir, KEYS_FILENAME)


DEFAULT_PROVIDER = "gemini"


def _clean(v) -> list[str]:
    """把任意值正規化成「去空白後非空」的字串清單。"""
    if not isinstance(v, list):
        return []
    return [str(k).strip() for k in v if str(k).strip()]


def _load_all(base_dir: str) -> dict[str, list[str]]:
    """讀回 {供應商: [金鑰...]}；檔案不存在、損毀或解密失敗都回 {}（不丟例外）。

    相容舊版單一池格式 ``{"keys": [...]}``——視為 ``gemini`` 供應商的金鑰。
    """
    path = _keys_path(base_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return {}
    try:
        if blob.startswith(_MAGIC_DPAPI):
            payload = _dpapi(blob[len(_MAGIC_DPAPI):], encrypt=False)
        elif blob.startswith(_MAGIC_B64):
            payload = base64.b64decode(blob[len(_MAGIC_B64):])
        else:
            return {}
        data = json.loads(payload.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    prov = data.get("providers")
    if isinstance(prov, dict):
        return {str(p): _clean(v) for p, v in prov.items() if _clean(v)}
    # 舊格式（單一池）→ 視為 gemini
    keys = _clean(data.get("keys"))
    return {DEFAULT_PROVIDER: keys} if keys else {}


def _save_all(base_dir: str, providers: dict[str, list[str]]) -> None:
    """把 {供應商: [金鑰...]} 加密寫入。全空則刪檔。"""
    providers = {str(p): _clean(v) for p, v in (providers or {}).items()
                 if _clean(v)}
    path = _keys_path(base_dir)
    if not providers:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return
    payload = json.dumps({"providers": providers},
                         ensure_ascii=False).encode("utf-8")
    if _IS_WIN:
        try:
            blob = _MAGIC_DPAPI + _dpapi(payload, encrypt=True)
        except OSError:
            blob = _MAGIC_B64 + base64.b64encode(payload)
    else:
        blob = _MAGIC_B64 + base64.b64encode(payload)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)


def save_keys(base_dir: str, keys: list[str],
              provider: str = DEFAULT_PROVIDER) -> None:
    """更新單一供應商的金鑰清單（其餘供應商保留）。空清單則移除該供應商。"""
    allp = _load_all(base_dir)
    keys = _clean(keys)
    if keys:
        allp[provider] = keys
    else:
        allp.pop(provider, None)
    _save_all(base_dir, allp)


def load_keys(base_dir: str, provider: str = DEFAULT_PROVIDER) -> list[str]:
    """讀回指定供應商的金鑰清單；無則回 []。"""
    return _load_all(base_dir).get(provider, [])


def load_all_keys(base_dir: str) -> dict[str, list[str]]:
    """一次讀回所有供應商的金鑰（供 UI 開啟時載入各家欄位）。"""
    return _load_all(base_dir)


def save_all_keys(base_dir: str, providers: dict[str, list[str]]) -> None:
    """一次覆寫所有供應商的金鑰（供 UI 儲存時一併寫入）。"""
    _save_all(base_dir, providers)


def is_real_encryption() -> bool:
    """目前平台是否提供真正的加密（DPAPI）。供 UI 顯示提示用。"""
    return _IS_WIN
