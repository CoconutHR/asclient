"""Local browser inspector for AScript device trees.

It intentionally has no runtime dependency: the browser speaks only to this
loopback server, which speaks to the configured device.
"""
from __future__ import annotations

import base64
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import AScriptClient


_PAGE = r'''<!doctype html><meta charset="utf-8"><title>ASClient Inspector</title>
<style>
*{box-sizing:border-box}body{margin:0;font:13px system-ui,sans-serif;color:#18212b;background:#edf1f4}header{height:48px;background:#18212b;color:#fff;display:flex;align-items:center;gap:10px;padding:0 14px}button,input,select{font:inherit}button{border:0;border-radius:4px;padding:6px 10px;background:#d1e7dd;color:#123;cursor:pointer}main{height:calc(100vh - 48px);display:grid;grid-template-columns:31% 43% 26%;gap:1px;background:#c8d0d6}.pane{min-width:0;overflow:auto;background:#fff;padding:12px}.tree button{width:100%;text-align:left;background:#fff;color:#18212b;padding:4px;border-radius:2px}.tree button:hover,.tree button.active{background:#d7eef6}.muted{color:#6b7785}.phone{position:relative;margin:auto;max-width:100%;background:#111;line-height:0}.phone img{width:100%;height:auto}.box{position:absolute;border:2px solid #00d084;background:#00d08422;pointer-events:none}.prop{display:grid;grid-template-columns:110px 1fr;gap:5px;border-bottom:1px solid #edf1f4;padding:5px 0;word-break:break-word}pre{white-space:pre-wrap;background:#f4f6f8;padding:9px;line-height:1.45}#message{margin-left:auto;color:#b9c9d9}
</style><header><strong>ASClient Inspector</strong><select id="mode"><option value="smart">Smart</option><option value="full">Full</option><option value="visible">Visible</option></select><button onclick="refresh()">Refresh</button><label><input id="live" type="checkbox"> Live</label><span id="message"></span></header><main><section class="pane"><input id="search" placeholder="Search tree" oninput="renderTree()" style="width:100%;padding:6px"><div id="tree" class="tree"></div></section><section class="pane"><div id="phone" class="phone"><img id="screen"></div></section><aside id="details" class="pane"><p class="muted">Select an element in the tree or on the screenshot.</p></aside></main>
<script>
let snapshot={},nodes=[],selected=null;const $=id=>document.getElementById(id);
function flatten(items,depth=0){for(const n of items||[]){n._depth=depth;n._key=nodes.length;nodes.push(n);flatten(n.childs,depth+1)}}
function label(n){return [n.type,n.name||n.label||n.value||n.text].filter(Boolean).join('  ')}
function rect(n){return n.rect||{left:n.x||0,top:n.y||0,right:(n.x||0)+(n.width||0),bottom:(n.y||0)+(n.height||0)}}
function renderTree(){let q=$('search').value.toLowerCase(),out='';for(const n of nodes){if(q&&!label(n).toLowerCase().includes(q))continue;out+=`<button class="${selected===n._key?'active':''}" style="padding-left:${n._depth*16+4}px" onclick="pick(${n._key})">${escapeHtml(label(n)||'(unnamed)')}</button>`}$('tree').innerHTML=out}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function pick(k){selected=k;let n=nodes[k],r=rect(n),cfg=snapshot.tree.config||{},w=(cfg.display||{}).widthPixels||1,h=(cfg.display||{}).heightPixels||1,phone=$('phone');let box=document.createElement('div');box.className='box';box.style.left=(r.left/w*100)+'%';box.style.top=(r.top/h*100)+'%';box.style.width=((r.right-r.left)/w*100)+'%';box.style.height=((r.bottom-r.top)/h*100)+'%';phone.querySelectorAll('.box').forEach(e=>e.remove());phone.append(box);let code=candidate(n);$('details').innerHTML='<h3>'+escapeHtml(label(n))+'</h3>'+Object.entries(n).filter(([k])=>!k.startsWith('_')&&k!=='childs'&&k!=='rect').map(([k,v])=>'<div class="prop"><b>'+escapeHtml(k)+'</b><span>'+escapeHtml(typeof v==='object'?JSON.stringify(v):v)+'</span></div>').join('')+'<h4>Python selector</h4><pre>'+escapeHtml(code)+'</pre><button onclick="copyCode()">Copy selector</button>';renderTree()}
function candidate(n){let base="device.selector()";if(n.name)return base+`.name(${JSON.stringify(n.name)})`;if(n.label)return base+`.label(${JSON.stringify(n.label)})`;if(n.value)return base+`.value(${JSON.stringify(n.value)})`;return base+`.type(${JSON.stringify(n.type)})`}
function copyCode(){navigator.clipboard.writeText(candidate(nodes[selected]));}
async function refresh(){try{$('message').textContent='Loading...';let r=await fetch('/api/snapshot?mode='+encodeURIComponent($('mode').value));if(!r.ok)throw new Error(await r.text());snapshot=await r.json();nodes=[];flatten(snapshot.tree.views);$('screen').src='data:image/png;base64,'+snapshot.image;$('message').textContent=nodes.length+' nodes';renderTree()}catch(e){$('message').textContent='Error: '+e.message}}
$('screen').addEventListener('click',e=>{let cfg=snapshot.tree.config||{},w=(cfg.display||{}).widthPixels||1,h=(cfg.display||{}).heightPixels||1,im=e.target,x=e.offsetX/im.clientWidth*w,y=e.offsetY/im.clientHeight*h,best=null;for(const n of nodes){let r=rect(n);if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom)best=n}if(best)pick(best._key)});
setInterval(()=>{if($('live').checked)refresh()},1500);refresh();
</script>'''


def serve(client: "AScriptClient", *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None: pass
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path == "/": self._send(200, _PAGE.encode(), "text/html; charset=utf-8"); return
            if path == "/api/snapshot":
                try:
                    mode = "smart"
                    for part in query.split("&"):
                        if part.startswith("mode="): mode = part.split("=", 1)[1]
                    tree = client.ui_tree(mode=mode)
                    data = json.dumps({"tree": tree, "image": base64.b64encode(client.screenshot()).decode("ascii")}, ensure_ascii=False).encode()
                    self._send(200, data, "application/json; charset=utf-8")
                except Exception as exc: self._send(502, str(exc).encode(), "text/plain; charset=utf-8")
                return
            self._send(404, b"Not found", "text/plain")
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{server.server_port}/"
    if open_browser: webbrowser.open(url)
    return server


def run_forever(client: "AScriptClient", *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> str:
    server = serve(client, host=host, port=port, open_browser=open_browser)
    url = f"http://{host}:{server.server_port}/"
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return url
