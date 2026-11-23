"""追更清單（v3.46）：記下連載中作品的某一話，檢查之後有沒有新的話。

判斷方式＝看網頁的上下話清單（各站解析器已把關聯記事整理成「舊 → 新」、當前話標
``is_current``）：基準話之後還有項目就是有更新。關聯清單常只列出附近十幾話，所以找到
新的話後再抓「目前看到最新的那一話」重看一次，最多 ``_MAX_HOPS`` 次，避免漏算。

只用 ``fetch_parse(url) -> (text, nav, title)`` 回呼取資料（呼叫端決定要不要走暫存——
檢查更新一定要重新上網抓），本模組不碰網路與 UI，方便測試。
"""
from __future__ import annotations

import datetime as _dt
from typing import Callable

_MAX_HOPS = 20         # 找到新的話後，最多再往後追幾頁（每頁約多看到 7～8 話；追滿仍有就標 more）


def norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def new_entry(url: str, title: str = "") -> dict:
    """新增一部作品。``url``＝基準話（通常是看到／翻到的最新一話）。"""
    return {"url": url.strip(), "title": title, "new": [], "checked_at": "",
            "error": ""}


def _newer_in(nav: list[dict], url: str) -> list[dict] | None:
    """上下話清單中排在 url（當前話）之後的項目；找不到當前話回 None。"""
    idx = next((i for i, x in enumerate(nav) if x.get("is_current")), None)
    if idx is None:
        idx = next((i for i, x in enumerate(nav)
                    if norm_url(x.get("url") or "") == norm_url(url)), None)
    if idx is None:
        return None
    return [{"title": x.get("title", ""), "url": x["url"]}
            for x in nav[idx + 1:] if x.get("url")]


def check_entry(entry: dict, fetch_parse: Callable[[str], tuple],
                stop: Callable[[], bool] | None = None) -> dict:
    """檢查一部作品，回傳更新後的 entry（不改傳入的 dict）。

    ``new``＝基準話之後的所有新話（舊 → 新）；``error``＝這次檢查失敗的原因（保留上次的
    ``new``，不因一次連線失敗就把已知的更新清掉）。
    """
    e = dict(entry)
    e["checked_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    base = e["url"]
    try:
        _text, nav, title = fetch_parse(base)
    except Exception as ex:  # noqa: BLE001 — 連線／解析錯誤都只記下來
        e["error"] = f"讀取失敗：{ex}"
        return e
    if title and not e.get("title"):
        e["title"] = title
    newer = _newer_in(nav or [], base)
    if newer is None:
        e["error"] = "這一頁的上下話清單裡找不到這一話，無法判斷有沒有更新"
        return e
    found: list[dict] = []
    seen = {norm_url(base)}
    for item in newer:
        if norm_url(item["url"]) not in seen:
            seen.add(norm_url(item["url"]))
            found.append(item)
    hops = 0
    more_left = False
    while found and not (stop and stop()):
        if hops >= _MAX_HOPS:
            more_left = True      # 還沒追完，實際新話可能更多
            break
        hops += 1
        tail = found[-1]["url"]
        try:
            _t, nav2, _ti = fetch_parse(tail)
        except Exception:  # noqa: BLE001 — 往後追失敗就以目前找到的為準
            break
        more = [x for x in (_newer_in(nav2 or [], tail) or [])
                if norm_url(x["url"]) not in seen]
        if not more:
            break
        for x in more:
            seen.add(norm_url(x["url"]))
            found.append(x)
    e["new"] = found
    e["more"] = more_left
    e["error"] = ""
    return e


def mark_read(entry: dict) -> dict:
    """看完了：基準話改成目前找到的最新一話，清掉新話清單。"""
    e = dict(entry)
    if e.get("new"):
        e["url"] = e["new"][-1]["url"]
        if e["new"][-1].get("title"):
            e["title"] = e["new"][-1]["title"]
        e["new"] = []
        e["more"] = False
    return e


def today() -> str:
    return _dt.date.today().isoformat()
