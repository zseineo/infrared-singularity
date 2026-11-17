"""手機遠端控制自動翻譯：程式內建的小型網頁伺服器（只用標準庫）。

手機瀏覽器開 ``http://<電腦 IP>:<埠>/?t=<密鑰>`` 就能看自動翻譯的狀態、Log，並開始／停止／
強制停止／繼續。只開放這幾個動作，其他功能（連線設定、金鑰…）一律不經網路暴露。

- **連線範圍**：綁 0.0.0.0，同一個 Wi-Fi 下用區網 IP；不在家時建議用 Tailscale 之類的
  私人網路連回電腦（不要在路由器開 port 對外）。
- **驗證**：密鑰 128 位元隨機字串，第一次帶 ``?t=`` 開啟後換成 HttpOnly＋SameSite=Strict 的
  cookie，之後的 API 都要它（也接受 ``X-Token`` 標頭）；比對用 ``hmac.compare_digest``。
  POST 一律要 JSON 本文，其他網站的頁面無法替你按按鈕（CSRF）。
- **執行緒**：伺服器在背景執行緒；讀狀態與執行動作都透過 ``snapshot``／``act`` 兩個回呼，
  由呼叫端（MainWindow）負責切回 Qt 主執行緒。
"""
from __future__ import annotations

import hmac
import json
import secrets
import socket
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


def access_links(port: int, token: str) -> list[str]:
    return [f"http://{ip}:{port}/?t={token}" for ip in local_ipv4s()]


class RemoteServer:
    """``snapshot(since)`` 回傳狀態 dict；``act(action, payload)`` 回傳 (ok, message)。"""

    def __init__(self, port: int, token: str,
                 snapshot: Callable[[int], dict],
                 act: Callable[[str, dict], tuple[bool, str]]) -> None:
        self.port = port
        self._token = token
        self._snapshot = snapshot
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
                if u.path == "/api/state":
                    if not self._authed(q):
                        self._json(401, {"error": "unauthorized"})
                        return
                    try:
                        since = int(q.get("since", ["0"])[0])
                    except ValueError:
                        since = 0
                    try:
                        self._json(200, server._snapshot(since))
                    except Exception as e:  # noqa: BLE001
                        self._json(500, {"error": str(e)})
                    return
                self._json(404, {"error": "not found"})

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
                action = u.path.rsplit("/", 1)[-1]
                if not u.path.startswith("/api/") or action not in (
                        "start", "stop", "force_stop", "resume"):
                    self._json(404, {"error": "not found"})
                    return
                try:
                    ok, msg = server._act(action, payload)
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
  --ok:#2ea043;--warn:#d29922;--bad:#da3633;--accent:#3b82f6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.45 -apple-system,"Segoe UI","Microsoft JhengHei","PingFang TC",sans-serif;
  padding:12px 16px calc(16px + env(safe-area-inset-bottom))}
h1{font-size:17px;margin:4px 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:13px;font-weight:600}
.idle{background:#3a3d43}.run{background:var(--ok)}.pause{background:var(--warn);color:#111}.stopping{background:var(--bad)}
.banner{margin-top:8px;word-break:break-all}
.dim{color:var(--dim);font-size:13px}
label{display:block;font-size:13px;color:var(--dim);margin:8px 0 4px}
input[type=text],input[type=number]{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);
  background:#1a1b1e;color:var(--fg);font-size:15px}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
button{padding:12px;border:0;border-radius:10px;font-size:15px;font-weight:600;color:#fff;background:var(--accent)}
button:disabled{opacity:.4}
.b-stop{background:#6b7280}.b-force{background:var(--bad)}.b-resume{background:var(--warn);color:#111}
#log{white-space:pre-wrap;word-break:break-all;font:12px/1.4 ui-monospace,Consolas,monospace;
  max-height:45vh;overflow:auto;background:#1a1b1e;border-radius:8px;padding:8px;margin:0}
details summary{cursor:pointer;color:var(--dim);font-size:14px}
ul{margin:6px 0 0;padding-left:18px;font-size:13px}
#msg{min-height:1.2em;margin-top:8px;font-size:14px}
.err{color:#ff7b72}.okm{color:#7ee787}
</style></head><body>
<h1>AATool 自動翻譯</h1>
<div class="card">
  <div class="row"><span id="badge" class="badge idle">閒置</span><span id="conn" class="dim"></span></div>
  <div id="banner" class="banner"></div>
  <div class="dim" style="margin-top:6px">當前：<span id="cur">—</span></div>
  <div class="dim">已完成 <b id="nd">0</b>　已跳過 <b id="ns">0</b></div>
  <div class="btns">
    <button id="bStop" class="b-stop">■ 停止</button>
    <button id="bForce" class="b-force">⏏ 強制停止</button>
    <button id="bResume" class="b-resume" style="grid-column:1/-1;display:none">▶ 繼續</button>
  </div>
  <div id="msg"></div>
</div>
<div class="card" id="startCard">
  <label for="url">起始網址</label>
  <input id="url" type="text" inputmode="url" autocomplete="off">
  <div class="row" style="margin-top:4px">
    <div style="flex:1"><label for="count">話數</label><input id="count" type="number" min="1" max="999"></div>
    <label style="margin-top:28px"><input id="until" type="checkbox"> 翻到最後一話</label>
  </div>
  <div class="dim" id="listNote" style="margin-top:6px"></div>
  <div class="btns"><button id="bStart" style="grid-column:1/-1">▶ 開始自動翻譯</button></div>
</div>
<div class="card">
  <details id="lists"><summary>已完成／已跳過清單</summary>
    <div class="dim" style="margin-top:6px">已完成</div><ul id="done"></ul>
    <div class="dim" style="margin-top:6px">已跳過</div><ul id="skip"></ul>
  </details>
</div>
<div class="card"><div class="row" style="justify-content:space-between">
  <b>Log</b><label style="margin:0"><input id="follow" type="checkbox" checked> 自動捲到底</label></div>
  <pre id="log"></pre></div>
<script>
const $=id=>document.getElementById(id);
let since=0,formFilled=false,busy=false,wasRunning=false;
function fill(ul,items){ul.innerHTML='';for(const t of items){const li=document.createElement('li');li.textContent=t;ul.appendChild(li);}}
async function post(action,body){
  busy=true;setBtns(window._st||{});$('msg').textContent='送出中…';$('msg').className='';
  try{const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body||{}),credentials:'same-origin'});
    const j=await r.json();$('msg').textContent=j.message||(j.ok?'完成':'失敗');$('msg').className=j.ok?'okm':'err';
  }catch(e){$('msg').textContent='連不到電腦：'+e;$('msg').className='err';}
  busy=false;poll();
}
function setBtns(s){
  const run=!!s.running;
  $('bStart').disabled=busy||run||s.thread_busy;
  $('bStop').disabled=busy||!run||s.stopping;
  $('bForce').disabled=busy||!run;
  $('bResume').style.display=s.paused?'':'none';$('bResume').disabled=busy;
  for(const id of ['url','count','until'])$(id).disabled=run;
}
async function poll(){
  try{
    const r=await fetch('/api/state?since='+since,{credentials:'same-origin',cache:'no-store'});
    if(r.status===401){$('conn').textContent='密鑰失效，請重新用電腦上的連結開啟';return;}
    const s=await r.json();window._st=s;$('conn').textContent='';
    const b=$('badge');
    if(s.paused){b.textContent='暫停中';b.className='badge pause';}
    else if(s.stopping){b.textContent='停止中';b.className='badge stopping';}
    else if(s.running){b.textContent='執行中';b.className='badge run';}
    else{b.textContent='閒置';b.className='badge idle';}
    $('banner').textContent=s.banner||'';$('cur').textContent=s.current||'—';
    $('nd').textContent=s.done.length;$('ns').textContent=s.skipped.length;
    fill($('done'),s.done);fill($('skip'),s.skipped);
    // 第一次載入、或一批剛結束（電腦會把接續網址回填到起始網址）時帶入表單
    if(!formFilled||(wasRunning&&!s.running)){
      $('url').value=s.form.url||'';$('count').value=s.form.count||1;$('until').checked=!!s.form.until_last;formFilled=true;}
    wasRunning=!!s.running;
    $('listNote').textContent=s.form.url_list?('電腦面板設了手動網址清單（'+s.form.url_list+' 個），會照清單翻，起始網址不用填'):'';
    if(s.log_reset){$('log').textContent='';}
    if(s.log.length){const lg=$('log');lg.textContent+=(lg.textContent?'\n':'')+s.log.join('\n');
      if(lg.textContent.length>200000)lg.textContent=lg.textContent.slice(-150000);
      if($('follow').checked)lg.scrollTop=lg.scrollHeight;}
    since=s.log_total;setBtns(s);
  }catch(e){$('conn').textContent='（連線中斷，重試中…）';}
}
$('bStart').onclick=()=>post('start',{url:$('url').value.trim(),count:parseInt($('count').value||'1',10),until_last:$('until').checked});
$('bStop').onclick=()=>post('stop');
$('bForce').onclick=()=>{if(confirm('立即結束？正在翻譯的這一話會捨棄、不存檔。'))post('force_stop');};
$('bResume').onclick=()=>post('resume');
poll();setInterval(()=>{if(!busy)poll();},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
</script></body></html>"""
