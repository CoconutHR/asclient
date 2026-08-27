"""Local browser inspector for AScript device trees.

It intentionally has no runtime dependency: the browser speaks only to this
loopback server, which speaks to the configured device.
"""
from __future__ import annotations

import base64
import json
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from .client import AScriptClient


_PAGE = r'''<!doctype html><meta charset="utf-8"><title>ASClient 控件检查器</title>
<style>
*{box-sizing:border-box}body{margin:0;font:13px system-ui,sans-serif;color:#18212b;background:#edf1f4}header{height:54px;background:#18212b;color:#fff;display:flex;align-items:center;gap:10px;padding:0 14px}button,input,select{font:inherit}button{border:0;border-radius:4px;padding:6px 10px;background:#d1e7dd;color:#123;cursor:pointer}main{height:calc(100vh - 54px);display:grid;grid-template-columns:31fr 6px 43fr 6px 26fr;background:#c8d0d6}.pane{min-width:0;overflow:auto;background:#fff;padding:12px}.divider{background:#c8d0d6;cursor:col-resize;touch-action:none}.divider:hover,.divider.dragging{background:#00a6a6}.tree button{width:100%;text-align:left;background:#fff;color:#18212b;padding:4px;border-radius:2px}.tree button:hover,.tree button.active{background:#d7eef6}.muted{color:#6b7785}.phone{position:relative;margin:auto;max-width:100%;background:#111;line-height:0}.phone img{display:block;width:100%;height:auto}.box{position:absolute;border:2px solid #00d084;background:#00d08422;pointer-events:none}.prop{display:grid;grid-template-columns:110px 1fr;gap:5px;border-bottom:1px solid #edf1f4;padding:5px 0;word-break:break-word}pre{white-space:pre-wrap;background:#f4f6f8;padding:9px;line-height:1.45}#appmeta{min-width:0;max-width:34%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#b9c9d9}#coordinate,#message{color:#b9c9d9;white-space:nowrap}#message{margin-left:auto}
</style><header><strong>ASClient 控件检查器</strong><span id="appmeta">正在等待设备快照...</span><span id="coordinate">动作坐标：--</span><select id="mode"><option value="smart">智能</option><option value="full">完整</option><option value="visible">可见</option></select><button onclick="refresh()">刷新</button><button id="crop" onclick="toggleCrop()">裁剪保存</button><label><input id="live" type="checkbox"> 实时刷新</label><span id="message"></span></header><main id="workspace"><section class="pane"><input id="search" placeholder="搜索控件树" oninput="renderTree()" style="width:100%;padding:6px"><div id="tree" class="tree"></div></section><div id="divider-left" class="divider" title="调整控件树面板宽度"></div><section class="pane"><div id="phone" class="phone"><img id="screen"></div></section><div id="divider-right" class="divider" title="调整属性面板宽度"></div><aside id="details" class="pane"><p class="muted">在控件树或截图中选择一个元素。</p></aside></main>
<script>
let snapshot={},nodes=[],selected=null,cropMode=false,cropStart=null,cropFinished=false;const $=id=>document.getElementById(id);
function flatten(items,depth=0){for(const n of items||[]){n._depth=depth;n._key=nodes.length;nodes.push(n);flatten(n.childs,depth+1)}}
function label(n){return [n.type,n.name||n.label||n.value||n.text].filter(Boolean).join('  ')}
function rect(n){return n.rect||{left:n.x||0,top:n.y||0,right:(n.x||0)+(n.width||0),bottom:(n.y||0)+(n.height||0)}}
function renderTree(){let q=$('search').value.toLowerCase(),out='';for(const n of nodes){if(q&&!label(n).toLowerCase().includes(q))continue;out+=`<button class="${selected===n._key?'active':''}" style="padding-left:${n._depth*16+4}px" onclick="pick(${n._key})">${escapeHtml(label(n)||'（未命名）')}</button>`}$('tree').innerHTML=out}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function actionSpace(){let s=snapshot.coordinate_space||{};return {width:s.width||1,height:s.height||1}}
function treeSpace(){let s=snapshot.tree_coordinate_space||{},a=actionSpace();return {width:s.width||a.width,height:s.height||a.height}}
function pick(k){selected=k;let n=nodes[k],r=rect(n),s=treeSpace(),phone=$('phone');let box=document.createElement('div');box.className='box';box.style.left=(r.left/s.width*100)+'%';box.style.top=(r.top/s.height*100)+'%';box.style.width=((r.right-r.left)/s.width*100)+'%';box.style.height=((r.bottom-r.top)/s.height*100)+'%';phone.querySelectorAll('.box').forEach(e=>e.remove());phone.append(box);let code=candidate(n);$('details').innerHTML='<h3>'+escapeHtml(label(n))+'</h3>'+Object.entries(n).filter(([k])=>!k.startsWith('_')&&k!=='childs'&&k!=='rect').map(([k,v])=>'<div class="prop"><b>'+escapeHtml(k)+'</b><span>'+escapeHtml(typeof v==='object'?JSON.stringify(v):v)+'</span></div>').join('')+'<h4>Python 选择器</h4><pre>'+escapeHtml(code)+'</pre><button onclick="copyCode()">复制选择器</button> <button onclick="verifySelector()">验证选择器</button><p id="verification" class="muted">尚未在真机上验证。</p>';renderTree()}
function selectorPayload(n){if(n.name)return {sel:[{key:'name',params:n.name}],find:99999};if(n.label)return {sel:[{key:'label',params:n.label}],find:99999};if(n.value)return {sel:[{key:'value',params:n.value}],find:99999};return {sel:[{key:'type',params:n.type}],find:99999}}
function candidate(n){let base="device.selector()";if(n.name)return base+`.name(${JSON.stringify(n.name)})`;if(n.label)return base+`.label(${JSON.stringify(n.label)})`;if(n.value)return base+`.value(${JSON.stringify(n.value)})`;return base+`.type(${JSON.stringify(n.type)})`}
function copyCode(){navigator.clipboard.writeText(candidate(nodes[selected]));}
async function verifySelector(){let target=$('verification'),n=nodes[selected];target.textContent='正在验证...';try{let u='/api/selector?mode='+encodeURIComponent($('mode').value)+'&selector='+encodeURIComponent(JSON.stringify(selectorPayload(n)));let response=await fetch(u);if(!response.ok)throw new Error(await response.text());let result=await response.json();target.textContent=result.count===1?'验证通过：唯一匹配。':`警告：当前匹配 ${result.count} 个元素，请细化选择器。`}catch(e){target.textContent='验证出错：'+e.message}}
function renderAppMeta(){let app=snapshot.app||{},name=app.name||'未知应用',bundle=app.bundle_id||'unknown.bundle',pid=app.pid==null?'?':app.pid;$('appmeta').textContent=`${name} | ${bundle} | PID ${pid}`;$('appmeta').title=$('appmeta').textContent}
async function refresh(){try{$('message').textContent='正在加载...';let r=await fetch('/api/snapshot?mode='+encodeURIComponent($('mode').value));if(!r.ok)throw new Error(await r.text());snapshot=await r.json();nodes=[];flatten(snapshot.tree.views);$('screen').src='data:image/png;base64,'+snapshot.image;renderAppMeta();$('message').textContent=nodes.length+' 个节点'+(snapshot.app_error?' | 前台应用信息不可用':'');renderTree()}catch(e){$('message').textContent='错误：'+e.message}}
$('screen').addEventListener('click',e=>{if(cropFinished){cropFinished=false;return}let action=actionSpace(),tree=treeSpace(),im=e.target,x=e.offsetX/im.clientWidth*action.width,y=e.offsetY/im.clientHeight*action.height,treeX=x/action.width*tree.width,treeY=y/action.height*tree.height,best=null;$('coordinate').textContent=`动作坐标：x=${x.toFixed(1)}, y=${y.toFixed(1)}`;for(const n of nodes){let r=rect(n);if(treeX>=r.left&&treeX<=r.right&&treeY>=r.top&&treeY<=r.bottom)best=n}if(best)pick(best._key)});
function toggleCrop(){cropMode=!cropMode;$('crop').textContent=cropMode?'请在截图上拖拽':'裁剪保存';$('crop').style.background=cropMode?'#ffd166':'';}
function cropPoint(e){let im=$('screen'),r=im.getBoundingClientRect();return {x:Math.max(0,Math.min(e.clientX-r.left,r.width)),y:Math.max(0,Math.min(e.clientY-r.top,r.height))}}
function cropBox(a,b){return {x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),w:Math.abs(a.x-b.x),h:Math.abs(a.y-b.y)}}
function showCropBox(box){let phone=$('phone'),el=phone.querySelector('.cropbox')||document.createElement('div');el.className='box cropbox';el.style.borderColor='#ff8c00';el.style.background='#ff8c0022';el.style.left=(box.x/$('screen').clientWidth*100)+'%';el.style.top=(box.y/$('screen').clientHeight*100)+'%';el.style.width=(box.w/$('screen').clientWidth*100)+'%';el.style.height=(box.h/$('screen').clientHeight*100)+'%';if(!el.parentNode)phone.append(el)}
async function saveCrop(box){let im=$('screen'),scaleX=im.naturalWidth/im.clientWidth,scaleY=im.naturalHeight/im.clientHeight,x=Math.round(box.x*scaleX),y=Math.round(box.y*scaleY),w=Math.max(1,Math.round(box.w*scaleX)),h=Math.max(1,Math.round(box.h*scaleY)),canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;canvas.getContext('2d').drawImage(im,x,y,w,h,0,0,w,h);let image=canvas.toDataURL('image/png').split(',')[1],r=await fetch('/api/crop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image})});if(!r.ok)throw new Error(await r.text());let result=await r.json();$('message').textContent='裁剪图已保存：'+result.path}
$('screen').addEventListener('pointerdown',e=>{if(!cropMode)return;cropStart=cropPoint(e);$('screen').setPointerCapture(e.pointerId);e.preventDefault()});$('screen').addEventListener('pointermove',e=>{if(cropStart)showCropBox(cropBox(cropStart,cropPoint(e)))});$('screen').addEventListener('pointerup',async e=>{if(!cropStart)return;let box=cropBox(cropStart,cropPoint(e));cropStart=null;if(box.w<3||box.h<3)return;cropFinished=true;$('message').textContent='正在保存裁剪图...';try{await saveCrop(box)}catch(err){$('message').textContent='裁剪失败：'+err.message}finally{cropMode=false;$('crop').textContent='裁剪保存'}});
function initializePanels(){let area=$('workspace'),usable=area.clientWidth-12;area.style.gridTemplateColumns=`${usable*.31}px 6px ${usable*.43}px 6px ${usable*.26}px`}
function panelWidths(){let values=getComputedStyle($('workspace')).gridTemplateColumns.split(' ').map(parseFloat);return {left:values[0],middle:values[2],right:values[4]}}
function beginResize(side,event){let area=$('workspace'),start=panelWidths(),startX=event.clientX,min=180,minMiddle=260,total=area.clientWidth-12,divider=$(side==='left'?'divider-left':'divider-right');divider.classList.add('dragging');divider.setPointerCapture(event.pointerId);function move(e){let dx=e.clientX-startX;if(side==='left'){let left=Math.max(min,Math.min(start.left+dx,total-minMiddle-min));let middle=total-left-start.right;area.style.gridTemplateColumns=`${left}px 6px ${middle}px 6px ${start.right}px`}else{let right=Math.max(min,Math.min(start.right-dx,total-minMiddle-min));let middle=total-start.left-right;area.style.gridTemplateColumns=`${start.left}px 6px ${middle}px 6px ${right}px`}}function end(){divider.classList.remove('dragging');divider.removeEventListener('pointermove',move);divider.removeEventListener('pointerup',end);divider.removeEventListener('pointercancel',end)}divider.addEventListener('pointermove',move);divider.addEventListener('pointerup',end);divider.addEventListener('pointercancel',end)}
$('divider-left').addEventListener('pointerdown',e=>beginResize('left',e));$('divider-right').addEventListener('pointerdown',e=>beginResize('right',e));window.addEventListener('load',initializePanels);
setInterval(()=>{if($('live').checked)refresh()},1500);refresh();
</script>'''


def serve(client: "AScriptClient", *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True, output_dir: str | Path | None = None) -> ThreadingHTTPServer:
    """Start the local Inspector; selected screenshot crops save into ``output_dir``."""
    crop_directory = Path(output_dir or Path.cwd()).resolve()
    crop_directory.mkdir(parents=True, exist_ok=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None: pass
        def _send(self, status: int, body: bytes, content_type: str) -> bool:
            """Return false when a browser abandons an in-flight response.

            This is expected during refresh/navigation, especially on Windows;
            attempting an error response on the same closed socket only creates
            noisy terminal tracebacks.
            """
            try:
                self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return False
            return True
        def do_GET(self) -> None:
            parsed = urlparse(self.path); path, query = parsed.path, parse_qs(parsed.query)
            if path == "/": self._send(200, _PAGE.encode(), "text/html; charset=utf-8"); return
            if path == "/api/snapshot":
                try:
                    mode = query.get("mode", ["smart"])[0]
                    tree = client.ui_tree(mode=mode)
                    app, app_error = {}, ""
                    try: app = client.current_app()
                    except Exception as exc: app_error = str(exc)
                    image = client.screenshot()
                    display = tree.get("config", {}).get("display", {}) if isinstance(tree, dict) else {}
                    tree_width = int(display.get("widthPixels") or 0)
                    tree_height = int(display.get("heightPixels") or 0)
                    width, height = 0, 0
                    if len(image) >= 24 and image.startswith(b"\x89PNG\r\n\x1a\n"):
                        width = int.from_bytes(image[16:20], "big")
                        height = int.from_bytes(image[20:24], "big")
                    width, height = width or tree_width or 1, height or tree_height or 1
                    data = json.dumps({"tree": tree, "app": app, "app_error": app_error, "coordinate_space": {"width": width, "height": height}, "tree_coordinate_space": {"width": tree_width or width, "height": tree_height or height}, "image": base64.b64encode(image).decode("ascii")}, ensure_ascii=False).encode()
                    self._send(200, data, "application/json; charset=utf-8")
                except Exception as exc: self._send(502, str(exc).encode(), "text/plain; charset=utf-8")
                return
            if path == "/api/selector":
                try:
                    mode = query.get("mode", ["smart"])[0]
                    selector = json.loads(query.get("selector", [""])[0])
                    if not isinstance(selector, dict): raise ValueError("selector must be a JSON object")
                    elements = client.find_elements(selector, mode=mode)
                    self._send(200, json.dumps({"count": len(elements), "elements": elements}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                except Exception as exc: self._send(502, str(exc).encode(), "text/plain; charset=utf-8")
                return
            self._send(404, b"Not found", "text/plain")
        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/crop": self._send(404, b"Not found", "text/plain"); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                image = base64.b64decode(str(payload.get("image", "")), validate=True)
                if len(image) < 24 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("crop must be a PNG image")
                width, height = int.from_bytes(image[16:20], "big"), int.from_bytes(image[20:24], "big")
                if width <= 0 or height <= 0 or len(image) > 25 * 1024 * 1024: raise ValueError("invalid crop dimensions or size")
                destination = crop_directory / f"inspect_crop_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
                destination.write_bytes(image)
                self._send(200, json.dumps({"path": str(destination)}).encode(), "application/json; charset=utf-8")
            except Exception as exc: self._send(400, str(exc).encode(), "text/plain; charset=utf-8")
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
