"""對外連線的共用設定：Proxy（代理伺服器）與 API 請求的 User-Agent。

為什麼需要：使用者回報在有網路封鎖的環境（實例：中國大陸連 FC2）抓網頁失敗，
症狀是 TLS 握手逾時與 `WinError 10061 連線被主動拒絕` 交替出現，但**瀏覽器打得開**
——因為代理只掛在瀏覽器上（擴充功能或 PAC），而本程式用 urllib 走直連。
urllib 只讀 Windows 登錄檔的系統代理、**不支援 PAC**，所以需要讓使用者手動指定。

**抓網頁與 API 翻譯分成兩個獨立設定**：被封鎖的常是來源站台（FC2 等），而 API
端點（例：api.deepseek.com）往往直連反而更快，兩者需求不同。因此這裡不動全域
狀態（環境變數／`install_opener`），而是各自建立 opener，由呼叫端指定要用哪個：

* `url_fetcher` 以 `set_fetch_proxy()` 記住抓網頁用的代理
* `GeminiApiSession` / `ChatApiSession` 以建構子參數 `proxy=` 取得 API 用的代理
"""
from __future__ import annotations

import urllib.request

#: API 請求用的 User-Agent。
#: **為什麼要指定**：不帶此標頭時 urllib 會自動補上 `Python-urllib/3.x`，
#: 部分 LLM API 中轉商（代理商）的防護會直接擋掉這種一看就是腳本的請求
#: （使用者實測：補上 User-Agent 後即可正常使用）。開頭的 `Mozilla/5.0`
#: 是為了通過那類只看前綴的過濾，尾端 `AATool` 仍誠實標明是本程式。
#: 與 `url_fetcher._HEADERS` 的 User-Agent **刻意分開**：抓 AA 故事網頁時是
#: 以瀏覽器身分抓公開網頁，兩者將來可能需要不同值。
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 AATool"

#: 已建立的 opener 快取（正規化後的代理位址 → opener）
_openers: dict[str, urllib.request.OpenerDirector] = {}


def normalize(proxy: str) -> str:
    """正規化使用者輸入：補上 scheme、去頭尾空白與尾斜線。空輸入回空字串。

    使用者常直接填 `127.0.0.1:7890`（Clash 預設）或 `127.0.0.1:10809`（v2rayN），
    urllib 需要帶 scheme 才認得，故預設補 `http://`——代理本身以 HTTP CONNECT
    轉送，即使被代理的是 https 流量也一樣。
    """
    p = (proxy or "").strip()
    if not p:
        return ""
    if "://" not in p:
        p = "http://" + p
    return p.rstrip("/")


def opener_for(proxy: str) -> urllib.request.OpenerDirector | None:
    """取得走該代理的 opener；`proxy` 為空回 None（呼叫端用預設 `urlopen`）。"""
    p = normalize(proxy)
    if not p:
        return None
    if p not in _openers:
        _openers[p] = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p}))
    return _openers[p]


def urlopen(req, *, timeout: float, proxy: str = ""):
    """`urllib.request.urlopen` 的替代：指定代理時走該代理，否則維持原行為。

    未指定代理時走預設 `urlopen`（仍會沿用 Windows 系統代理設定），
    行為與加入本模組之前完全相同。
    """
    op = opener_for(proxy)
    if op is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return op.open(req, timeout=timeout)
