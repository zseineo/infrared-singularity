"""網址連線診斷（手動版）。

平常不需要跑這支：抓取失敗時，程式**會自己**把同一份診斷印進自動翻譯 Log
／網址讀取的狀態列（見 `aa_tool/url_fetcher.diagnose_connection`）。
這支是給「想主動測一下」或「要在沒跑翻譯的情況下確認網路」時用的。

用法
----
    py -3.12 check_url_fetch.py                     # 用內建的預設網址
    py -3.12 check_url_fetch.py <網址> [<網址> ...]  # 指定要測的網址
"""
from __future__ import annotations

import os
import ssl
import sys
import time

DEFAULT_URLS = [
    "https://iitokolo.blog.fc2.com/blog-entry-2842.html",
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from aa_tool import url_fetcher

    print(f"Python {sys.version.split()[0]} / {ssl.OPENSSL_VERSION}")
    for url in sys.argv[1:] or DEFAULT_URLS:
        print()
        print(f"═══ {url}")
        for line in url_fetcher.diagnose_connection(url):
            print(line)
        t0 = time.time()
        try:
            page = url_fetcher.fetch_url(url)
            print(f"  抓取 ✅ 成功，{len(page)} 字元"
                  f"（{time.time() - t0:.1f} 秒）")
        except Exception as e:  # noqa: BLE001 — 診斷工具要印出任何失敗
            print(f"  抓取 ❌ {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
