"""手機遠端控制自動翻譯：程式內建的小型網頁伺服器（只用標準庫）。

手機瀏覽器開 ``http://<電腦 IP>:<埠>/?t=<密鑰>`` 就能操作自動翻譯：狀態／Log／開始停止
（「狀態」分頁）、整個自動翻譯面板與連線設定（「面板」「連線設定」分頁，由
``aa_remote_mirror_qt`` 自動鏡像電腦上的元件）、網址讀取（讀取、上下話、作者／忽略留言／
不讀暫存）。API 金鑰一律不經網路（面板上標 ``remoteSkip``）。

- **連線範圍**：綁 0.0.0.0，同一個 Wi-Fi 下用區網 IP；不在家時用 Tailscale 連回電腦
  （不要在路由器開 port 對外）。
- **驗證**：密鑰 128 位元隨機字串，第一次帶 ``?t=`` 開啟後換成 HttpOnly＋SameSite=Strict 的
  cookie，之後的 API 都要它（也接受 ``X-Token`` 標頭）；比對用 ``hmac.compare_digest``。
  POST 一律要 JSON 本文，其他網站的頁面無法替你按按鈕（CSRF）。
- **路由**：``GET /api/<名稱>``（``GET_NAMES``）→ ``query``；``POST /api/<名稱>``
  （``POST_NAMES``）→ ``act``。兩個回呼由呼叫端（MainWindow）負責切回 Qt 主執行緒。
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

DEFAULT_PORT = 8765
COOKIE = "aat_token"
_MAX_BODY = 64 * 1024


def new_token() -> str:
    return secrets.token_urlsafe(16)


def local_ipv4s() -> list[str]:
    """這台電腦的 IPv4（區網、Tailscale 的 100.x 等），預設路由那張網卡排第一。"""
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # 不會真的送出封包，只為取得預設網卡 IP
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")] or ["127.0.0.1"]


def _tailscale_exe() -> str:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", "")):
        p = os.path.join(base, "Tailscale", "tailscale.exe") if base else ""
        if p and os.path.isfile(p):
            return p
    return ""


def tailscale_info(timeout: float = 3.0) -> tuple[list[str], str]:
    """向 Tailscale CLI 問本機的 Tailscale IPv4 與 MagicDNS 名稱；沒裝／沒登入回 ([], "")。

    系統解析主機名稱（``local_ipv4s``）不一定列得出 Tailscale 網卡的 100.x，直接問最準。
    """
    exe = _tailscale_exe()
    if not exe:
        return [], ""
    try:
        r = subprocess.run(
            [exe, "status", "--json"], capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], ""
    if data.get("BackendState") != "Running":
        return [], ""
    me = data.get("Self") or {}
    ips = [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
    return ips, str(me.get("DNSName") or "").rstrip(".")


def _is_tailscale_ip(ip: str) -> bool:
    """100.64.0.0/10（Tailscale 用的 CGNAT 網段）。"""
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def access_links(port: int, token: str) -> list[tuple[str, str]]:
    """(說明, 連結)：Tailscale（在家在外都能用）排前面，再來是區網位址。"""
    ts_ips, ts_name = tailscale_info()
    out: list[tuple[str, str]] = []
    if ts_name:
        out.append(("Tailscale 名稱", f"http://{ts_name}:{port}/?t={token}"))
    seen: set[str] = set()
    for ip in ts_ips + [ip for ip in local_ipv4s() if _is_tailscale_ip(ip)]:
        if ip not in seen:
            seen.add(ip)
            out.append(("Tailscale", f"http://{ip}:{port}/?t={token}"))
    for ip in local_ipv4s():
        if ip in seen or ip.startswith("169.254."):   # 169.254＝沒拿到位址的網卡
            continue
        seen.add(ip)
        out.append(("同一個 Wi-Fi／區網", f"http://{ip}:{port}/?t={token}"))
    return out


#: GET /api/<名稱>（唯讀查詢）與 POST /api/<名稱>（動作）允許的名稱
GET_NAMES = {"state", "ui", "fs", "history"}
POST_NAMES = {"start", "stop", "force_stop", "resume", "read", "from_file",
              "ui_set", "ui_click", "conn_open", "folder", "reorder", "fetchopts"}


class RemoteServer:
    """``query(name, params)`` 回傳 dict；``act(name, payload)`` 回傳 (ok, message)。"""

    def __init__(self, port: int, token: str,
                 query: Callable[[str, dict], dict],
                 act: Callable[[str, dict], tuple[bool, str]]) -> None:
        self.port = port
        self._token = token
        self._query = query
        self._act = act
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AATool"
            sys_version = ""

            def log_message(self, *_a) -> None:   # 不印到 console
                pass

            def _authed(self, query: dict) -> bool:
                cand = (query.get("t", [""])[0] or self.headers.get("X-Token", "")
                        or self._cookie_token())
                return bool(cand) and hmac.compare_digest(cand, server._token)

            def _cookie_token(self) -> str:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE:
                        return v
                return ""

            def _send(self, code: int, body: bytes, ctype: str,
                      extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_GET(self) -> None:   # noqa: N802
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/":
                    if not self._authed(q):
                        self._send(401, _DENIED_HTML.encode("utf-8"), "text/html; charset=utf-8")
                        return
                    extra = {}
                    if "t" in q:   # 換成 cookie，網址列不留密鑰
                        extra["Set-Cookie"] = (f"{COOKIE}={server._token}; HttpOnly; "
                                               "SameSite=Strict; Path=/; Max-Age=31536000")
                    self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8", extra)
                    return
                name = u.path[len("/api/"):] if u.path.startswith("/api/") else ""
                if name not in GET_NAMES:
                    self._json(404, {"error": "not found"})
                    return
                if not self._authed(q):
                    self._json(401, {"error": "unauthorized"})
                    return
                params = {k: v[0] for k, v in q.items() if k != "t"}
                try:
                    self._json(200, server._query(name, params))
                except Exception as e:  # noqa: BLE001
                    self._json(500, {"error": str(e)})

            def do_POST(self) -> None:   # noqa: N802
                u = urllib.parse.urlparse(self.path)
                if not self._authed({}):
                    self._json(401, {"error": "unauthorized"})
                    return
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    self._json(415, {"error": "需要 JSON"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n > _MAX_BODY:
                    self._json(413, {"error": "too large"})
                    return
                try:
                    payload = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError
                except ValueError:
                    self._json(400, {"error": "bad json"})
                    return
                name = u.path[len("/api/"):] if u.path.startswith("/api/") else ""
                if name not in POST_NAMES:
                    self._json(404, {"error": "not found"})
                    return
                try:
                    ok, msg = server._act(name, payload)
                except Exception as e:  # noqa: BLE001
                    ok, msg = False, str(e)
                self._json(200, {"ok": ok, "message": msg})

        return Handler


_DENIED_HTML = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AATool 遠端</title>
<body style="font-family:sans-serif;background:#1e1f22;color:#ddd;padding:24px">
<h3>需要存取密鑰</h3><p>請用電腦上「⚙ 設定 → 手機遠端控制」顯示的完整連結開啟。</p></body>"""

PAGE_HTML = r"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1e1f22">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>AATool 自動翻譯</title>
<style>
:root{--bg:#1e1f22;--card:#2b2d31;--line:#3a3d43;--fg:#e3e5e8;--dim:#9aa0a6;
  --ok:#2ea043;--warn:#d29922;--bad:#da3633;--accent:#3b82f6;--field:#1a1b1e}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.45 -apple-system,"Segoe UI","Microsoft JhengHei","PingFang TC",sans-serif;
  padding:0 16px calc(16px + env(safe-area-inset-bottom))}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0 8px}
h1{font-size:16px;margin:0 0 8px;display:flex;gap:8px;align-items:center}
.tabs{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;background:var(--card);border-radius:10px;padding:4px}
.tabs button{background:none;color:var(--dim);padding:8px 0;font-size:14px;border-radius:8px}
.tabs button.on{background:var(--accent);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:12px 0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:13px;font-weight:600}
.idle{background:#3a3d43}.run{background:var(--ok)}.pause{background:var(--warn);color:#111}.stopping{background:var(--bad)}
.banner{margin-top:8px;word-break:break-all}
.dim{color:var(--dim);font-size:13px}
label.fl{display:block;font-size:13px;color:var(--dim);margin:10px 0 4px}
input[type=text],input[type=number],select,textarea{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);
  background:var(--field);color:var(--fg);font-size:15px;font-family:inherit}
textarea{min-height:120px;resize:vertical;font-size:13px}
input:disabled,select:disabled,textarea:disabled{opacity:.45}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
button{padding:11px 12px;border:0;border-radius:10px;font-size:15px;font-weight:600;color:#fff;background:var(--accent)}
button:disabled{opacity:.4}
button.sec{background:#4b5058}button.on2{background:var(--ok)}
.b-stop{background:#6b7280}.b-force{background:var(--bad)}.b-resume{background:var(--warn);color:#111}
.chk{display:flex;gap:10px;align-items:flex-start;padding:8px 0}
.chk input{width:20px;height:20px;margin-top:1px;flex:none}
.tip{color:var(--dim);font-size:12px;white-space:pre-wrap;margin-top:4px;display:none}
.tip.show{display:block}
.i{display:inline-block;width:18px;height:18px;line-height:18px;text-align:center;border-radius:50%;
  background:#3a3d43;color:var(--dim);font-size:12px;margin-left:6px;cursor:pointer;font-style:normal}
.heading{font-weight:700;margin:16px 0 4px;font-size:15px}
.txt{color:var(--dim);font-size:13px;white-space:pre-wrap;margin:6px 0}
.txt.b{color:var(--fg);font-weight:600;font-size:15px}
hr{border:0;border-top:1px solid var(--line);margin:14px 0}
.ctl{margin:4px 0}
.inline{display:flex;gap:8px;flex-wrap:wrap}
.inline>button{flex:1 1 auto}
#log{white-space:pre-wrap;word-break:break-all;font:12px/1.4 ui-monospace,Consolas,monospace;
  max-height:45vh;overflow:auto;background:var(--field);border-radius:8px;padding:8px;margin:0}
details summary{cursor:pointer;color:var(--dim);font-size:14px}
ul{margin:6px 0 0;padding-left:18px;font-size:13px}
#msg{min-height:1.2em;margin-top:8px;font-size:14px}
.err{color:#ff7b72}.okm{color:#7ee787}
.chapters{max-height:45vh;overflow:auto;margin-top:8px;border-radius:8px;background:var(--field)}
.chapters a{display:block;padding:9px 10px;border-bottom:1px solid var(--line);color:var(--fg);
  text-decoration:none;font-size:14px;word-break:break-all}
.chapters a.cur{background:#1f3b63;font-weight:600}
table.tw{width:100%;border-collapse:collapse;font-size:13px}
table.tw td,table.tw th{border-bottom:1px solid var(--line);padding:4px;vertical-align:middle}
table.tw input[type=text],table.tw select{padding:6px;font-size:13px}
.sheet{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20;display:none;align-items:flex-end}
.sheet.show{display:flex}
.sheet>div{background:var(--card);width:100%;max-height:88vh;overflow:auto;border-radius:16px 16px 0 0;
  padding:16px 16px calc(16px + env(safe-area-inset-bottom))}
.dirs a{display:block;padding:11px 8px;border-bottom:1px solid var(--line);color:var(--fg);text-decoration:none;word-break:break-all}
.chips{display:flex;flex-wrap:wrap;gap:6px;max-height:136px;overflow:auto;margin-top:8px}
.chips button{padding:6px 10px;font-size:13px;font-weight:400;background:#3a3f44;color:#ddd;border-radius:999px}
.chips button.on{background:var(--accent);color:#fff}
.hidden{display:none!important}
</style></head><body>
<header>
  <h1>AATool 自動翻譯 <span id="badge" class="badge idle">閒置</span><span id="conn" class="dim"></span></h1>
  <div class="tabs">
    <button data-tab="status" class="on">狀態</button><button data-tab="auto">面板</button>
    <button data-tab="conn">連線設定</button><button data-tab="fetch">網址讀取</button>
  </div>
</header>

<section id="tab-status">
  <div class="card">
    <div id="banner" class="banner"></div>
    <div class="dim" style="margin-top:6px">當前：<span id="cur">—</span></div>
    <div class="dim">已完成 <b id="nd">0</b>　已跳過 <b id="ns">0</b></div>
    <div class="btns">
      <button id="bStart" style="grid-column:1/-1">▶ 開始自動翻譯</button>
      <button id="bStop" class="b-stop">■ 停止</button>
      <button id="bForce" class="b-force">⏏ 強制停止</button>
      <button id="bResume" class="b-resume" style="grid-column:1/-1;display:none">▶ 繼續</button>
    </div>
    <div class="dim" style="margin-top:8px">起始網址等設定在「面板」分頁；選話在「網址讀取」分頁。</div>
    <div id="msg"></div>
  </div>
  <div class="card">
    <details><summary>已完成／已跳過清單</summary>
      <div class="dim" style="margin-top:6px">已完成</div><ul id="done"></ul>
      <div class="dim" style="margin-top:6px">已跳過</div><ul id="skip"></ul>
    </details>
  </div>
  <div class="card"><div class="row" style="justify-content:space-between">
    <b>Log</b><label class="dim"><input id="follow" type="checkbox" checked> 自動捲到底</label></div>
    <pre id="log"></pre></div>
</section>

<section id="tab-auto" class="hidden"><div class="card" id="form-auto"><div class="dim">載入中…</div></div></section>
<section id="tab-conn" class="hidden">
  <div class="card" id="form-conn"><div class="dim">載入中…</div></div>
  <div class="dim" style="margin:0 4px 12px">API 金鑰只能在電腦上修改。改完記得按「💾 儲存連線設定」。</div>
</section>

<section id="tab-fetch" class="hidden">
  <div class="card">
    <label class="fl" for="furl">網址</label>
    <div class="row" style="flex-wrap:nowrap">
      <input id="furl" type="text" inputmode="url" autocomplete="off" style="flex:1">
      <button id="bRead" style="white-space:nowrap">🌐 讀取</button>
    </div>
    <div class="inline" style="margin-top:8px">
      <button id="bPrev" class="sec">◀ 上一話</button><button id="bNext" class="sec">下一話 ▶</button>
    </div>
    <button id="bFile" class="sec" style="width:100%;margin-top:8px">📄 從檔案（用電腦上已存的檔案找回網址）</button>
    <label class="fl" for="fauthor">作者名稱</label>
    <input id="fauthor" type="text" autocomplete="off">
    <label class="chk"><input id="fonly" type="checkbox"><span>忽略留言（只留作者貼文）</span></label>
    <label class="chk"><input id="fdice" type="checkbox"><span>保留骰子留言（忽略留言時，有【1D10:7】這類骰子的留言照留）</span></label>
    <label class="chk"><input id="fskip" type="checkbox"><span>不讀暫存（每次重新上網抓）</span></label>
    <div class="dim" style="margin-top:6px">檔名：<span id="pvName">—</span></div>
    <div class="dim" id="pvFolderRow" style="display:none">作品資料夾：<span id="pvFolder"></span></div>
    <div class="dim">讀取後網址會帶到電腦面板的「起始網址」（翻譯中不會改）。</div>
  </div>
  <div class="card" id="readCard" style="display:none">
    <div id="rdTitle" style="font-weight:600;word-break:break-all"></div>
    <div id="rdStatus" class="dim" style="margin-top:4px"></div>
    <div id="rdList" class="chapters"></div>
  </div>
  <div class="card">
    <b>讀取紀錄</b>
    <input id="hq" type="text" placeholder="搜尋標題（或點下面的作品名）" autocomplete="off" style="margin-top:8px">
    <div id="hchips" class="chips"></div>
    <div id="hinfo" class="dim" style="margin-top:6px"></div>
    <div id="hlist" class="chapters"></div>
    <button id="hmore" class="sec" style="width:100%;margin-top:8px;display:none"></button>
  </div>
</section>

<div class="sheet" id="modal"><div id="modalBody"></div></div>
<div class="sheet" id="picker"><div>
  <div id="pkTitle" style="font-weight:700">選擇資料夾</div>
  <div id="pkPath" class="dim" style="word-break:break-all;margin:6px 0"></div>
  <div id="pkDirs" class="dirs"></div>
  <div class="btns"><button id="pkCancel" class="sec">取消</button><button id="pkOk">選這個資料夾</button></div>
</div></div>

<script>
const $=id=>document.getElementById(id);
let tab='status',since=0,busy=false,readSeq=-1,readLoading=null,optsFilled=false,st={};
const sigs={},els={auto:{},conn:{},modal:{}};
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
async function api(name,body){
  const r=await fetch('/api/'+name,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{}),credentials:'same-origin'});
  return r.json();
}
async function post(name,body,quiet){
  busy=true;setBtns();if(!quiet){$('msg').textContent='送出中…';$('msg').className='';}
  let j={};
  try{j=await api(name,body);
    if(!quiet||!j.ok){toast(j.message||(j.ok?'完成':'失敗'),j.ok);}
  }catch(e){toast('連不到電腦：'+e,false);}
  busy=false;poll();if(tab==='auto'||tab==='conn')loadUI(tab);
  return j;
}
function toast(m,ok){$('msg').textContent=m;$('msg').className=ok?'okm':'err';
  if(tab!=='status'){const t=$('conn');t.textContent=' '+m;t.className=ok?'okm':'err';
    clearTimeout(window._tt);window._tt=setTimeout(()=>{t.textContent='';t.className='dim';},4000);}}
function setBtns(){
  const run=!!st.running;
  $('bStart').disabled=busy||run||st.thread_busy;
  $('bStop').disabled=busy||!run||st.stopping;$('bForce').disabled=busy||!run;
  $('bResume').style.display=st.paused?'':'none';$('bResume').disabled=busy;
  $('bRead').disabled=$('bFile').disabled=busy||(st.read&&st.read.loading);
}
// ── 狀態輪詢 ──
async function poll(){
  try{
    const r=await fetch('/api/state?since='+since,{credentials:'same-origin',cache:'no-store'});
    if(r.status===401){$('conn').textContent=' 密鑰失效，請重新用電腦上的連結開啟';return;}
    st=await r.json();
    const b=$('badge');
    if(st.paused){b.textContent='暫停中';b.className='badge pause';}
    else if(st.stopping){b.textContent='停止中';b.className='badge stopping';}
    else if(st.running){b.textContent='執行中';b.className='badge run';}
    else{b.textContent='閒置';b.className='badge idle';}
    $('banner').textContent=st.banner||'';$('cur').textContent=st.current||'—';
    $('nd').textContent=st.done.length;$('ns').textContent=st.skipped.length;
    fill($('done'),st.done);fill($('skip'),st.skipped);
    if(st.log_reset)$('log').textContent='';
    if(st.log.length){const lg=$('log');lg.textContent+=(lg.textContent?'\n':'')+st.log.join('\n');
      if(lg.textContent.length>200000)lg.textContent=lg.textContent.slice(-150000);
      if($('follow').checked)lg.scrollTop=lg.scrollHeight;}
    since=st.log_total;
    renderRead(st.read);
    const pv=st.preview||{};$('pvName').textContent=pv.filename||'—';
    $('pvFolderRow').style.display=pv.folder?'':'none';$('pvFolder').textContent=pv.folder||'';
    if(!optsFilled&&st.fetchopts){$('fauthor').value=st.fetchopts.author||'';$('fonly').checked=!!st.fetchopts.author_only;$('fdice').checked=!!st.fetchopts.keep_dice;$('fdice').disabled=!$('fonly').checked;
      $('fskip').checked=!!st.fetchopts.skip_cache;if(!$('furl').value)$('furl').value=(st.form&&st.form.url)||'';optsFilled=true;}
    renderModal(st.modal);
    setBtns();
  }catch(e){$('conn').textContent=' （連線中斷，重試中…）';}
}
function fill(ul,items){ul.innerHTML='';for(const t of items){const li=document.createElement('li');li.textContent=t;ul.appendChild(li);}}
// ── 面板鏡像 ──
async function loadUI(view){
  try{const r=await fetch('/api/ui?view='+view,{credentials:'same-origin',cache:'no-store'});
    const j=await r.json();if(j.error){$('form-'+view).innerHTML='<div class="err">'+esc(j.error)+'</div>';return;}
    renderItems($('form-'+view),j.items,view);
  }catch(e){}
}
function sigOf(items){return JSON.stringify(items.map(it=>[it.kind,it.id,it.label,it.text,it.options,it.radio,it.special,it.readonly,
  it.heads,it.rows&&it.rows.length,it.rows&&it.rows.map(r=>r.map(c=>c.kind+(c.id||''))),it.items&&it.items.length]));}
function renderItems(box,items,view){
  const s=sigOf(items);
  if(sigs[view]!==s){sigs[view]=s;els[view]={};box.innerHTML='';for(const it of items)box.appendChild(build(it,view));}
  for(const it of items)update(it,view);
}
function tipEl(it,host){if(!it.tip)return;const i=document.createElement('i');i.className='i';i.textContent='i';
  const t=document.createElement('div');t.className='tip';t.textContent=it.tip;i.onclick=()=>t.classList.toggle('show');host.appendChild(i);return t;}
function build(it,view){
  const w=document.createElement('div');w.className='ctl';
  if(it.kind==='heading'){w.className='heading';w.textContent=it.text;return w;}
  if(it.kind==='sep'){return document.createElement('hr');}
  if(it.kind==='text'){w.className='txt'+(it.bold?' b':'');w.textContent=it.text;return w;}
  if(it.kind==='list'){const ul=document.createElement('ul');for(const x of it.items){const li=document.createElement('li');li.textContent=x;ul.appendChild(li);}w.appendChild(ul);return w;}
  let lab=null;
  if(it.label&&it.kind!=='check'){lab=document.createElement('label');lab.className='fl';lab.textContent=it.label;w.appendChild(lab);}
  let el,tipBox=null;
  const send=(v,extra)=>post('ui_set',Object.assign({id:it.id,value:v},extra||{}),true);
  if(it.kind==='line'){
    el=document.createElement('input');el.type='text';el.placeholder=it.placeholder||'';el.autocomplete='off';
    el.onchange=()=>send(el.value);w.appendChild(el);
  }else if(it.kind==='multiline'){
    el=document.createElement('textarea');el.placeholder=it.placeholder||'';el.onchange=()=>send(el.value);w.appendChild(el);
  }else if(it.kind==='combo'){
    if(it.editable){el=document.createElement('input');el.type='text';const dl=document.createElement('datalist');
      dl.id='dl'+it.id;for(const o of it.options){const op=document.createElement('option');op.value=o;dl.appendChild(op);}
      el.setAttribute('list',dl.id);el.onchange=()=>{const i=it.options.indexOf(el.value);send(i>=0?i:el.value);};w.appendChild(el);w.appendChild(dl);}
    else{el=document.createElement('select');it.options.forEach((o,i)=>{const op=document.createElement('option');op.value=i;op.textContent=o;el.appendChild(op);});
      el.onchange=()=>send(parseInt(el.value,10));w.appendChild(el);}
  }else if(it.kind==='check'){
    const l=document.createElement('label');l.className='chk';el=document.createElement('input');el.type='checkbox';
    const sp=document.createElement('span');sp.textContent=it.text||it.label;l.appendChild(el);l.appendChild(sp);
    el.onchange=()=>send(el.checked);w.appendChild(l);tipBox=tipEl(it,sp);
  }else if(it.kind==='spin'){
    const row=document.createElement('div');row.className='row';el=document.createElement('input');el.type='number';
    el.min=it.min;el.max=it.max;el.step=it.float?'any':it.step;el.style.flex='1';el.onchange=()=>send(it.float?parseFloat(el.value):parseInt(el.value,10));
    row.appendChild(el);if(it.suffix){const s=document.createElement('span');s.className='dim';s.textContent=it.suffix;row.appendChild(s);}w.appendChild(row);
  }else if(it.kind==='button'){
    el=document.createElement('button');el.className='sec';el.style.width='100%';el.textContent=it.text;
    el.onclick=()=>{
      if(it.special==='folder')openPicker('@out',p=>post('folder',{id:it.target,path:p}));
      else if(it.special==='reorder')openPicker('@reorder',p=>post('reorder',{path:p}));
      else post('ui_click',{id:it.id},true);};
    w.appendChild(el);
  }else if(it.kind==='table'){
    el=document.createElement('table');el.className='tw';w.appendChild(el);
  }
  if(lab&&it.kind!=='check')tipBox=tipEl(it,lab);
  if(tipBox)w.appendChild(tipBox);
  if(el)els[view][it.id]=el;
  return w;
}
function update(it,view){
  const el=it.id&&els[view][it.id];if(!el)return;
  el.disabled=!it.enabled||!!it.readonly;
  if(document.activeElement===el)return;   // 正在輸入的不蓋掉
  if(it.kind==='line'||it.kind==='multiline'){if(el.value!==it.value)el.value=it.value;}
  else if(it.kind==='combo'){if(it.editable){if(el.value!==it.value)el.value=it.value;}else el.value=String(it.index);}
  else if(it.kind==='check')el.checked=!!it.checked;
  else if(it.kind==='spin'){if(String(el.value)!==String(it.value))el.value=it.value;}
  else if(it.kind==='button'){el.textContent=it.text;el.className=it.checkable&&it.checked?'on2':'sec';}
  else if(it.kind==='table')renderTable(el,it,view);
}
function renderTable(t,it,view){
  if(t.contains(document.activeElement))return;
  t.innerHTML='';const hr=document.createElement('tr');hr.innerHTML='<th></th>'+it.heads.map(h=>'<th>'+esc(h)+'</th>').join('');t.appendChild(hr);
  it.rows.forEach((cells,r)=>{const tr=document.createElement('tr');
    const sel=document.createElement('td');const rb=document.createElement('input');rb.type='radio';rb.name='sel'+it.id;rb.checked=r===it.current;
    rb.onchange=()=>post('ui_set',{id:it.id,value:'',row:r,col:0,select:true},true);sel.appendChild(rb);tr.appendChild(sel);
    cells.forEach((c,ci)=>{const td=document.createElement('td');
      if(c.kind==='cell'){const inp=document.createElement('input');inp.type='text';inp.value=c.value;
        inp.onchange=()=>post('ui_set',{id:it.id,value:inp.value,row:r,col:ci},true);td.appendChild(inp);}
      else if(c.kind==='combo'){const s=document.createElement('select');c.options.forEach((o,i)=>{const op=document.createElement('option');op.value=i;op.textContent=o;s.appendChild(op);});
        s.value=String(c.index);s.onchange=()=>post('ui_set',{id:c.id,value:parseInt(s.value,10)},true);td.appendChild(s);}
      else td.textContent=c.text||c.value||'';
      tr.appendChild(td);});
    t.appendChild(tr);});
}
// ── 電腦上跳出的對話框 ──
let modalSig='';
function renderModal(m){
  const sh=$('modal');
  if(!m){sh.classList.remove('show');modalSig='';return;}
  const s=JSON.stringify(m.kind==='message'?m:[m.title,sigOf(m.items)]);
  sh.classList.add('show');
  const body=$('modalBody');
  if(s!==modalSig){modalSig=s;body.innerHTML='';sigs.modal=null;
    const h=document.createElement('div');h.style.fontWeight='700';h.textContent=(m.title||'電腦上的對話框');body.appendChild(h);
    if(m.kind==='message'){
      const p=document.createElement('div');p.className='txt b';p.style.fontWeight='400';p.textContent=m.text+(m.info?'\n\n'+m.info:'');body.appendChild(p);
      const bs=document.createElement('div');bs.className='btns';
      for(const b of m.buttons){const x=document.createElement('button');x.textContent=b.text;x.onclick=()=>post('ui_click',{id:b.id},true);bs.appendChild(x);}
      body.appendChild(bs);
    }else{const f=document.createElement('div');f.id='form-modal';body.appendChild(f);}
  }
  if(m.kind==='dialog')renderItems($('form-modal'),m.items,'modal');
}
// ── 資料夾瀏覽 ──
let pickCb=null,pickPath='',pickFiles=false;
// files=true：選檔案模式（列 html／htm／txt，點檔案＝選定；不顯示「選這個資料夾」）
async function openPicker(start,cb,files){pickCb=cb;pickFiles=!!files;
  $('pkTitle').textContent=pickFiles?'選擇檔案':'選擇資料夾';$('pkOk').style.display=pickFiles?'none':'';
  $('picker').classList.add('show');await listDir(start);}
async function listDir(p){
  $('pkDirs').innerHTML='<div class="dim">讀取中…</div>';
  try{const r=await fetch('/api/fs?path='+encodeURIComponent(p)+(pickFiles?'&files=1':''),{credentials:'same-origin'});const j=await r.json();
    pickPath=j.path||'';$('pkPath').textContent=pickPath||'（選擇磁碟）';const box=$('pkDirs');box.innerHTML='';
    if(j.error){box.innerHTML='<div class="err">'+esc(j.error)+'</div>';}
    if(pickPath){const up=document.createElement('a');up.href='#';up.textContent='⬆ 上一層';up.onclick=e=>{e.preventDefault();listDir(j.parent||'');};box.appendChild(up);}
    for(const d of j.dirs){const a=document.createElement('a');a.href='#';a.textContent='📁 '+d.name;a.onclick=e=>{e.preventDefault();listDir(d.path);};box.appendChild(a);}
    for(const f of (j.files||[])){const a=document.createElement('a');a.href='#';a.textContent='📄 '+f.name;
      a.onclick=e=>{e.preventDefault();$('picker').classList.remove('show');if(pickCb)pickCb(f.path);};box.appendChild(a);}
    if(pickFiles&&pickPath&&!(j.files||[]).length&&!j.error){const d=document.createElement('div');d.className='dim';d.style.padding='8px';
      d.textContent='（這個資料夾沒有 html／txt 檔）';box.appendChild(d);}
    $('pkOk').disabled=!pickPath;
  }catch(e){$('pkDirs').innerHTML='<div class="err">讀取失敗</div>';}
}
$('pkCancel').onclick=()=>$('picker').classList.remove('show');
$('pkOk').onclick=()=>{$('picker').classList.remove('show');if(pickCb&&pickPath)pickCb(pickPath);};
// ── 網址讀取 ──
function renderRead(r){
  if(!r||!r.seq)return;
  if(r.seq===readSeq&&r.loading===readLoading)return;
  readSeq=r.seq;readLoading=r.loading;
  $('readCard').style.display='';$('rdTitle').textContent=r.title||r.url||'';
  if(!r.loading&&$('furl').value!==r.url)$('furl').value=r.url;
  const s=$('rdStatus');
  if(r.loading){s.textContent='讀取中…';s.className='dim';}
  else if(r.error){s.textContent=r.error;s.className='err';}
  else{s.textContent='內文 '+r.lines+' 行，上下話 '+(r.chapters||[]).length+' 筆（點一下讀取該話）';s.className='dim';}
  const box=$('rdList');box.innerHTML='';let curEl=null;
  for(const c of (r.chapters||[])){const a=document.createElement('a');a.href='#';a.textContent=(c.current?'▶ ':'')+c.title;
    if(c.current){a.className='cur';curEl=a;}
    a.onclick=e=>{e.preventDefault();readUrl(c.url);};box.appendChild(a);}
  if(curEl)box.scrollTop=curEl.offsetTop-box.offsetTop-box.clientHeight/3;
}
function readUrl(u){if(!u)return;$('furl').value=u;post('read',{url:u},true);}
function stepChapter(d){const r=st.read||{};const ch=r.chapters||[];const i=ch.findIndex(c=>c.current);
  if(i<0||!ch[i+d]){toast(d<0?'沒有上一話':'沒有下一話',false);return;}readUrl(ch[i+d].url);}
$('bRead').onclick=()=>readUrl($('furl').value.trim());
$('bPrev').onclick=()=>stepChapter(-1);$('bNext').onclick=()=>stepChapter(1);
$('bFile').onclick=()=>openPicker('@last',p=>post('from_file',{path:p}),true);
// ── 讀取紀錄（同電腦網址讀取視窗：搜尋標題、作品名快速篩選、點一筆＝讀取）──
let histOff=0,histTitles=[],histTimer=null,histSeq=0;
async function loadHist(more,withTitles){
  const q=$('hq').value.trim(),off=more?histOff:0,my=++histSeq;
  try{const r=await fetch('/api/history?q='+encodeURIComponent(q)+'&offset='+off+(withTitles?'&titles=1':''),
      {credentials:'same-origin',cache:'no-store'});
    const j=await r.json();if(my!==histSeq)return;   // 打字中，只留最後一次的結果
    if(j.error){$('hinfo').textContent=j.error;return;}
    if(j.titles){histTitles=j.titles;renderChips();}
    const box=$('hlist');if(!more)box.innerHTML='';
    for(const e of j.entries){const a=document.createElement('a');a.href='#';a.textContent=e.title;
      a.onclick=ev=>{ev.preventDefault();readUrl(e.url);};box.appendChild(a);}
    histOff=off+j.entries.length;
    $('hinfo').textContent=j.total?(q?'符合 '+j.total+' 筆':'共 '+j.total+' 筆')+'（點一下讀取）':(q?'（無符合「'+q+'」的紀錄）':'（沒有讀取紀錄）');
    const m=$('hmore'),rest=j.total-histOff;m.style.display=rest>0?'':'none';m.textContent='顯示更多（尚有 '+rest+' 筆）';
  }catch(e){$('hinfo').textContent='讀取紀錄載入失敗';}
}
function renderChips(){
  const box=$('hchips'),q=$('hq').value.trim();box.innerHTML='';
  for(const t of histTitles){const b=document.createElement('button');b.textContent=t;b.className=t===q?'on':'';
    b.onclick=()=>{$('hq').value=($('hq').value.trim()===t)?'':t;markChips();loadHist(false,false);};box.appendChild(b);}
}
function markChips(){const q=$('hq').value.trim();for(const b of $('hchips').children)b.className=b.textContent===q?'on':'';}
$('hq').oninput=()=>{markChips();clearTimeout(histTimer);histTimer=setTimeout(()=>loadHist(false,false),300);};
$('hmore').onclick=()=>loadHist(true,false);
function sendOpts(){$('fdice').disabled=!$('fonly').checked;
  post('fetchopts',{author:$('fauthor').value.trim(),author_only:$('fonly').checked,keep_dice:$('fdice').checked,skip_cache:$('fskip').checked},true);}
for(const id of ['fauthor','fonly','fdice','fskip'])$(id).onchange=sendOpts;
// ── 狀態分頁按鈕 ──
$('bStart').onclick=()=>post('start',{});
$('bStop').onclick=()=>post('stop');
$('bForce').onclick=()=>{if(confirm('立即結束？正在翻譯的這一話會捨棄、不存檔。'))post('force_stop');};
$('bResume').onclick=()=>post('resume');
// ── 分頁 ──
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=async()=>{
  tab=b.dataset.tab;document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));
  for(const t of ['status','auto','conn','fetch'])$('tab-'+t).classList.toggle('hidden',t!==tab);
  if(tab==='conn')await api('conn_open',{});
  if(tab==='auto'||tab==='conn')loadUI(tab);
  if(tab==='fetch')loadHist(false,true);   // 每次切過來重抓（電腦上可能讀了新的網址）
});
poll();
setInterval(()=>{if(busy)return;poll();if(tab==='auto'||tab==='conn')loadUI(tab);},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
</script></body></html>"""
