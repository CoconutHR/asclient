"""Local browser inspector for AScript device trees.

It intentionally has no runtime dependency: the browser speaks only to this
loopback server, which speaks to the configured device.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import webbrowser
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from .client import AScriptClient


_PAGE = r'''<!doctype html><meta charset="utf-8"><title>ASClient 控件检查器</title>
<style>
*{box-sizing:border-box}body{margin:0;font:13px system-ui,sans-serif;color:#18212b;background:#edf1f4}header{min-height:54px;background:#18212b;color:#fff;display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:9px 14px}button,input,select{font:inherit}button{border:0;border-radius:4px;padding:6px 10px;background:#d1e7dd;color:#123;cursor:pointer}button:disabled{opacity:.48;cursor:not-allowed}button.secondary{background:#d7eef6}button.warning{background:#ffd166}main{height:calc(100vh - 72px);min-height:400px;display:grid;grid-template-columns:31fr 6px 43fr 6px 26fr;background:#c8d0d6}.pane{min-width:0;overflow:auto;background:#fff;padding:12px}.divider{background:#c8d0d6;cursor:col-resize;touch-action:none}.divider:hover,.divider.dragging{background:#00a6a6}.tree button{width:100%;text-align:left;background:#fff;color:#18212b;padding:4px;border-radius:2px}.tree button:hover,.tree button.active{background:#d7eef6}.muted{color:#6b7785}.phone{position:relative;margin:auto;max-width:100%;background:#111;line-height:0;touch-action:none}.phone.selecting{cursor:crosshair}.phone img{display:block;width:100%;height:auto;user-select:none}.elementbox,.cropbox{position:absolute;pointer-events:none}.elementbox{border:2px solid #00a6a6;background:#00a6a622}.cropbox{border:2px solid #ff8c00;background:#ff8c0022}.prop{display:grid;grid-template-columns:110px 1fr;gap:5px;border-bottom:1px solid #edf1f4;padding:5px 0;word-break:break-word}pre{white-space:pre-wrap;background:#f4f6f8;padding:9px;line-height:1.45}#appmeta{min-width:0;max-width:28%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#b9c9d9}#coordinate,#message,#cropstate{color:#b9c9d9;white-space:nowrap}#message{margin-left:auto}.selection-panel{margin:10px auto 0;max-width:100%;border:1px solid #d7dee5;border-radius:5px;background:#f7f9fb;padding:9px;line-height:1.55}.selection-panel strong{font-weight:600}.selection-panel pre{margin:7px 0 0}.selection-panel .hint{color:#6b7785}
</style><header><strong>ASClient 控件检查器</strong><span id="appmeta">正在等待设备快照...</span><span id="coordinate">动作坐标：--</span><span id="cropstate">浏览模式</span><select id="mode"><option value="smart">智能</option><option value="full">完整</option><option value="visible">可见</option></select><button class="secondary" onclick="refresh()">刷新</button><button id="crop-start" class="warning" onclick="startCrop()">框选区域</button><button id="crop-cancel" onclick="cancelCrop()" disabled>取消</button><button id="crop-save" onclick="saveCrop()" disabled>保存 PNG</button><button id="crop-copy" onclick="copyCrop()" disabled>复制坐标</button><label><input id="live" type="checkbox"> 实时刷新</label><span id="message"></span></header><main id="workspace"><section class="pane"><input id="search" placeholder="搜索控件树" oninput="renderTree()" style="width:100%;padding:6px"><div id="tree" class="tree"></div></section><div id="divider-left" class="divider" title="调整控件树面板宽度"></div><section class="pane"><div id="phone" class="phone"><img id="screen" draggable="false"></div><div id="selection-panel" class="selection-panel"><strong>区域截屏</strong><div class="hint">点击“框选区域”后会冻结当前 PNG；拖动只更新选区，点击“保存 PNG”才写入文件。</div></div></section><div id="divider-right" class="divider" title="调整属性面板宽度"></div><aside id="details" class="pane"><p class="muted">在控件树或截图中选择一个元素。</p></aside></main>
<script>
let snapshot={},nodes=[],selected=null,cropMode=false,freezeReady=false,cropStart=null,cropRect=null,suppressNextClick=false,refreshing=false,refreshSequence=0,liveBeforeCrop=false;const $=id=>document.getElementById(id);
function flatten(items,depth=0){for(const n of items||[]){n._depth=depth;n._key=nodes.length;nodes.push(n);flatten(n.childs,depth+1)}}
function label(n){return [n.type,n.name||n.label||n.value||n.text].filter(Boolean).join('  ')}
function rect(n){return n.rect||{left:n.x||0,top:n.y||0,right:(n.x||0)+(n.width||0),bottom:(n.y||0)+(n.height||0)}}
function renderTree(){let q=$('search').value.toLowerCase(),out='';for(const n of nodes){if(q&&!label(n).toLowerCase().includes(q))continue;out+=`<button class="${selected===n._key?'active':''}" style="padding-left:${n._depth*16+4}px" onclick="pick(${n._key})">${escapeHtml(label(n)||'（未命名）')}</button>`}$('tree').innerHTML=out}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function actionSpace(){let s=snapshot.coordinate_space||{};return {width:s.width||1,height:s.height||1}}
function treeSpace(){let s=snapshot.tree_coordinate_space||{},a=actionSpace();return {width:s.width||a.width,height:s.height||a.height}}
function pick(k){selected=k;let n=nodes[k],r=rect(n),s=treeSpace(),phone=$('phone');let box=phone.querySelector('.elementbox')||document.createElement('div');box.className='elementbox';box.style.left=(r.left/s.width*100)+'%';box.style.top=(r.top/s.height*100)+'%';box.style.width=((r.right-r.left)/s.width*100)+'%';box.style.height=((r.bottom-r.top)/s.height*100)+'%';if(!box.parentNode)phone.append(box);let code=candidate(n);$('details').innerHTML='<h3>'+escapeHtml(label(n))+'</h3>'+Object.entries(n).filter(([k])=>!k.startsWith('_')&&k!=='childs'&&k!=='rect').map(([k,v])=>'<div class="prop"><b>'+escapeHtml(k)+'</b><span>'+escapeHtml(typeof v==='object'?JSON.stringify(v):v)+'</span></div>').join('')+'<h4>Python 选择器</h4><pre>'+escapeHtml(code)+'</pre><button onclick="copyCode()">复制选择器</button> <button onclick="verifySelector()">验证选择器</button><p id="verification" class="muted">尚未在真机上验证。</p>';renderTree()}
function selectorPayload(n){if(n.name)return {sel:[{key:'name',params:n.name}],find:99999};if(n.label)return {sel:[{key:'label',params:n.label}],find:99999};if(n.value)return {sel:[{key:'value',params:n.value}],find:99999};return {sel:[{key:'type',params:n.type}],find:99999}}
function candidate(n){let base='device.selector()';if(n.name)return base+`.name(${JSON.stringify(n.name)})`;if(n.label)return base+`.label(${JSON.stringify(n.label)})`;if(n.value)return base+`.value(${JSON.stringify(n.value)})`;return base+`.type(${JSON.stringify(n.type)})`}
function copyCode(){navigator.clipboard.writeText(candidate(nodes[selected]));}
async function verifySelector(){let target=$('verification'),n=nodes[selected];target.textContent='正在验证...';try{let u='/api/selector?mode='+encodeURIComponent($('mode').value)+'&selector='+encodeURIComponent(JSON.stringify(selectorPayload(n)));let response=await fetch(u);if(!response.ok)throw new Error(await response.text());let result=await response.json();target.textContent=result.count===1?'验证通过：唯一匹配。':`警告：当前匹配 ${result.count} 个元素，请细化选择器。`}catch(e){target.textContent='验证出错：'+e.message}}
function renderAppMeta(){let app=snapshot.app||{},name=app.name||'未知应用',bundle=app.bundle_id||'unknown.bundle',pid=app.pid==null?'?':app.pid;$('appmeta').textContent=`${name} | ${bundle} | PID ${pid}`;$('appmeta').title=$('appmeta').textContent}
function setMessage(text){$('message').textContent=text}
function setScreenImage(image){let target=$('screen');return new Promise((resolve,reject)=>{let cleanup=()=>{target.removeEventListener('load',loaded);target.removeEventListener('error',failed)};let loaded=()=>{cleanup();resolve()};let failed=()=>{cleanup();reject(new Error('截图无法显示'))};target.addEventListener('load',loaded,{once:true});target.addEventListener('error',failed,{once:true});target.src='data:image/png;base64,'+image;if(target.complete&&target.naturalWidth){cleanup();resolve()}})}
async function refresh(){if(cropMode||refreshing)return;let request=++refreshSequence;refreshing=true;try{setMessage('正在加载...');let r=await fetch('/api/snapshot?mode='+encodeURIComponent($('mode').value),{cache:'no-store'});if(!r.ok)throw new Error(await r.text());let value=await r.json();if(request!==refreshSequence)return;snapshot=value;nodes=[];flatten(snapshot.tree.views);await setScreenImage(snapshot.image);if(request!==refreshSequence)return;renderAppMeta();setMessage(nodes.length+' 个节点'+(snapshot.app_error?' | 前台应用信息不可用':''));renderTree()}catch(e){setMessage('错误：'+e.message)}finally{if(request===refreshSequence)refreshing=false}}
function imagePoint(e){let im=$('screen'),r=im.getBoundingClientRect(),space=actionSpace();return {x:Math.max(0,Math.min(space.width,Math.round((e.clientX-r.left)/r.width*space.width))),y:Math.max(0,Math.min(space.height,Math.round((e.clientY-r.top)/r.height*space.height)))}}
function showCoordinate(point){$('coordinate').textContent=`动作坐标：x=${point.x}, y=${point.y}`}
function cropFromPoints(one,two){return {left:Math.min(one.x,two.x),top:Math.min(one.y,two.y),right:Math.max(one.x,two.x),bottom:Math.max(one.y,two.y)}}
function validCrop(value){return value&&value.left<value.right&&value.top<value.bottom}
function renderCrop(){let phone=$('phone'),box=phone.querySelector('.cropbox');if(!validCrop(cropRect)){if(box)box.remove();renderCropInfo();return}let space=actionSpace();if(!box){box=document.createElement('div');box.className='cropbox';phone.append(box)}box.style.left=(cropRect.left/space.width*100)+'%';box.style.top=(cropRect.top/space.height*100)+'%';box.style.width=((cropRect.right-cropRect.left)/space.width*100)+'%';box.style.height=((cropRect.bottom-cropRect.top)/space.height*100)+'%';renderCropInfo()}
function cropText(value){let space=actionSpace(),width=value.right-value.left,height=value.bottom-value.top;let relative=[value.left/space.width,value.top/space.height,value.right/space.width,value.bottom/space.height].map(n=>n.toFixed(6));return {width,height,relative,code:`region = (${value.left}, ${value.top}, ${value.right}, ${value.bottom})\nregion_relative = (${relative.join(', ')})\n# 尺寸: ${width} x ${height} px`}}
function renderCropInfo(){let panel=$('selection-panel'),save=$('crop-save'),copy=$('crop-copy');if(!cropMode){panel.innerHTML='<strong>区域截屏</strong><div class="hint">点击“框选区域”后会冻结当前 PNG；拖动只更新选区，点击“保存 PNG”才写入文件。</div>';save.disabled=true;copy.disabled=true;return}if(!freezeReady){panel.innerHTML='<strong>正在冻结 PNG</strong><div class="hint">等待新截图显示完成后才可框选，避免保存到旧画面。</div>';save.disabled=true;copy.disabled=true;return}if(!validCrop(cropRect)){panel.innerHTML='<strong>正在框选冻结 PNG</strong><div class="hint">拖动鼠标形成区域；Esc 或“取消”可退出，不会写入文件。</div>';save.disabled=true;copy.disabled=true;return}let info=cropText(cropRect);panel.innerHTML='<strong>已选区域</strong><div>物理像素：('+cropRect.left+', '+cropRect.top+') → ('+cropRect.right+', '+cropRect.bottom+')</div><div>尺寸：'+info.width+' × '+info.height+' px</div><div>中心：('+(cropRect.left+info.width/2).toFixed(1)+', '+(cropRect.top+info.height/2).toFixed(1)+')</div><div>相对坐标：('+info.relative.join(', ')+')</div><pre>'+escapeHtml(info.code)+'</pre>';save.disabled=false;copy.disabled=false}
async function startCrop(){if(refreshing)return;liveBeforeCrop=$('live').checked;$('live').checked=false;cropMode=true;freezeReady=false;cropStart=null;cropRect=null;$('phone').classList.add('selecting');$('crop-start').disabled=true;$('crop-cancel').disabled=false;$('cropstate').textContent='正在获取冻结 PNG...';renderCrop();await forceFrozenSnapshot()}
async function forceFrozenSnapshot(){let request=++refreshSequence;try{setMessage('正在冻结 PNG 截图...');let r=await fetch('/api/snapshot?mode='+encodeURIComponent($('mode').value),{cache:'no-store'});if(!r.ok)throw new Error(await r.text());let value=await r.json();if(request!==refreshSequence||!cropMode)return;snapshot=value;nodes=[];flatten(snapshot.tree.views);await setScreenImage(snapshot.image);if(request!==refreshSequence||!cropMode)return;renderAppMeta();renderTree();freezeReady=true;$('cropstate').textContent='已冻结 PNG，拖动框选';setMessage('截图已冻结，拖动只更新选区');renderCrop()}catch(e){setMessage('冻结失败：'+e.message);cancelCrop()}}
function cancelCrop(restoreLive=true){cropMode=false;freezeReady=false;cropStart=null;cropRect=null;$('phone').classList.remove('selecting');let box=$('phone').querySelector('.cropbox');if(box)box.remove();$('crop-start').disabled=false;$('crop-cancel').disabled=true;$('cropstate').textContent='浏览模式';renderCropInfo();if(restoreLive&&liveBeforeCrop){$('live').checked=true;refresh()}liveBeforeCrop=false}
async function saveCrop(){if(!freezeReady||!validCrop(cropRect)||!snapshot.snapshot_id)return;$('crop-save').disabled=true;setMessage('正在保存原始 PNG 裁剪...');try{let r=await fetch('/api/crop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({snapshot_id:snapshot.snapshot_id,rect:cropRect})});if(!r.ok)throw new Error(await r.text());let result=await r.json();setMessage('已保存：'+result.path+'；元数据：'+result.metadata_path);$('cropstate').textContent='区域已保存'}catch(e){setMessage('保存失败：'+e.message)}finally{$('crop-save').disabled=false}}
async function copyCrop(){if(!validCrop(cropRect))return;try{await navigator.clipboard.writeText(cropText(cropRect).code);setMessage('已复制区域坐标')}catch(e){setMessage('复制失败：'+e.message)}}
$('screen').addEventListener('pointermove',e=>{let point=imagePoint(e);showCoordinate(point);if(cropMode&&cropStart){cropRect=cropFromPoints(cropStart,point);renderCrop()}});
$('screen').addEventListener('pointerdown',e=>{if(!cropMode||!freezeReady)return;cropStart=imagePoint(e);cropRect=null;$('screen').setPointerCapture(e.pointerId);e.preventDefault();renderCrop()});
$('screen').addEventListener('pointerup',e=>{if(!cropMode||!freezeReady||!cropStart)return;cropRect=cropFromPoints(cropStart,imagePoint(e));cropStart=null;suppressNextClick=true;renderCrop();if(validCrop(cropRect)){$('cropstate').textContent='已选区域，可保存 PNG';setMessage('选区已更新，确认后点击“保存 PNG”')}else{setMessage('选区过小，请重新拖动')}});
$('screen').addEventListener('pointercancel',()=>{cropStart=null;renderCrop()});
$('screen').addEventListener('click',e=>{if(suppressNextClick){suppressNextClick=false;return}if(cropMode)return;let action=actionSpace(),tree=treeSpace(),point=imagePoint(e),treeX=point.x/action.width*tree.width,treeY=point.y/action.height*tree.height,best=null;showCoordinate(point);for(const n of nodes){let r=rect(n);if(treeX>=r.left&&treeX<=r.right&&treeY>=r.top&&treeY<=r.bottom)best=n}if(best)pick(best._key)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&cropMode){e.preventDefault();cancelCrop()}else if(e.key==='Enter'&&cropMode&&freezeReady&&validCrop(cropRect)){e.preventDefault();saveCrop()}});
function initializePanels(){let area=$('workspace'),usable=area.clientWidth-12;area.style.gridTemplateColumns=`${usable*.31}px 6px ${usable*.43}px 6px ${usable*.26}px`}
function panelWidths(){let values=getComputedStyle($('workspace')).gridTemplateColumns.split(' ').map(parseFloat);return {left:values[0],middle:values[2],right:values[4]}}
function beginResize(side,event){let area=$('workspace'),start=panelWidths(),startX=event.clientX,min=180,minMiddle=260,total=area.clientWidth-12,divider=$(side==='left'?'divider-left':'divider-right');divider.classList.add('dragging');divider.setPointerCapture(event.pointerId);function move(e){let dx=e.clientX-startX;if(side==='left'){let left=Math.max(min,Math.min(start.left+dx,total-minMiddle-min));let middle=total-left-start.right;area.style.gridTemplateColumns=`${left}px 6px ${middle}px 6px ${start.right}px`}else{let right=Math.max(min,Math.min(start.right-dx,total-minMiddle-min));let middle=total-start.left-right;area.style.gridTemplateColumns=`${start.left}px 6px ${middle}px 6px ${right}px`}}function end(){divider.classList.remove('dragging');divider.removeEventListener('pointermove',move);divider.removeEventListener('pointerup',end);divider.removeEventListener('pointercancel',end)}divider.addEventListener('pointermove',move);divider.addEventListener('pointerup',end);divider.addEventListener('pointercancel',end)}
$('divider-left').addEventListener('pointerdown',e=>beginResize('left',e));$('divider-right').addEventListener('pointerdown',e=>beginResize('right',e));window.addEventListener('load',initializePanels);
setInterval(()=>{if($('live').checked&&!cropMode)refresh()},1500);refresh();
</script>'''


_MAX_SNAPSHOTS = 6
_SNAPSHOT_TTL_SECONDS = 300.0
_MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
_MAX_SNAPSHOT_CACHE_BYTES = 48 * 1024 * 1024
_MAX_CROP_REQUEST_BYTES = 16 * 1024


def serve(client: "AScriptClient", *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True, output_dir: str | Path | None = None) -> ThreadingHTTPServer:
    """Start the local Inspector; selected screenshot crops save into ``output_dir``.

    Every Inspector snapshot is briefly retained in memory. Crop requests refer
    to this frozen, original PNG by ID, so the saved image and reported physical
    coordinates always describe the same frame.
    """
    crop_directory = Path(output_dir or Path.cwd()).resolve()
    crop_directory.mkdir(parents=True, exist_ok=True)
    snapshots: OrderedDict[str, dict[str, Any]] = OrderedDict()
    snapshots_lock = threading.RLock()
    crop_lock = threading.Lock()

    def now_iso() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def discard_expired_snapshots() -> None:
        cutoff = datetime.now().timestamp() - _SNAPSHOT_TTL_SECONDS
        for snapshot_id, item in list(snapshots.items()):
            if float(item["created_at_epoch"]) < cutoff:
                snapshots.pop(snapshot_id, None)

    def remembered_snapshot_bytes() -> int:
        return sum(len(item["image"]) for item in snapshots.values())

    def remember_snapshot(image: bytes, *, width: int, height: int, metadata: dict[str, Any]) -> str:
        if len(image) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("Inspector screenshot is too large to retain for region cropping")
        with snapshots_lock:
            discard_expired_snapshots()
            snapshot_id = secrets.token_urlsafe(18)
            snapshots[snapshot_id] = {
                "image": image,
                "width": width,
                "height": height,
                "created_at_epoch": datetime.now().timestamp(),
                "metadata": metadata,
            }
            while len(snapshots) > _MAX_SNAPSHOTS or remembered_snapshot_bytes() > _MAX_SNAPSHOT_CACHE_BYTES:
                snapshots.popitem(last=False)
            return snapshot_id

    def frozen_snapshot(snapshot_id: object) -> dict[str, Any] | None:
        if not isinstance(snapshot_id, str) or not snapshot_id:
            return None
        with snapshots_lock:
            discard_expired_snapshots()
            item = snapshots.get(snapshot_id)
            if item is not None:
                snapshots.move_to_end(snapshot_id)
            return item

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None: pass

        def _send(self, status: int, body: bytes, content_type: str) -> bool:
            """Return false when a browser abandons an in-flight response."""
            try:
                self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path); path, query = parsed.path, parse_qs(parsed.query)
            if path == "/":
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
                return
            if path == "/api/snapshot":
                try:
                    mode = query.get("mode", ["smart"])[0]
                    with client.locked():
                        image = client.screenshot()
                        tree = client.ui_tree(mode=mode)
                        app, app_error = {}, ""
                        try:
                            app = client.current_app()
                        except Exception as exc:
                            app_error = str(exc)
                    display = tree.get("config", {}).get("display", {}) if isinstance(tree, dict) else {}
                    tree_width = int(display.get("widthPixels") or 0)
                    tree_height = int(display.get("heightPixels") or 0)
                    size = client._png_size(image)
                    width, height = (int(size[0]), int(size[1])) if size is not None else (tree_width or 1, tree_height or 1)
                    captured_at = now_iso()
                    metadata = {"captured_at": captured_at, "mode": mode, "app": app, "app_error": app_error}
                    snapshot_id = remember_snapshot(image, width=width, height=height, metadata=metadata)
                    data = json.dumps({"snapshot_id": snapshot_id, "captured_at": captured_at, "tree": tree, "app": app, "app_error": app_error, "coordinate_space": {"width": width, "height": height}, "tree_coordinate_space": {"width": tree_width or width, "height": tree_height or height}, "image": base64.b64encode(image).decode("ascii")}, ensure_ascii=False).encode()
                    self._send(200, data, "application/json; charset=utf-8")
                except Exception as exc:
                    self._send(502, str(exc).encode(), "text/plain; charset=utf-8")
                return
            if path == "/api/selector":
                try:
                    mode = query.get("mode", ["smart"])[0]
                    selector = json.loads(query.get("selector", [""])[0])
                    if not isinstance(selector, dict):
                        raise ValueError("selector must be a JSON object")
                    elements = client.find_elements(selector, mode=mode)
                    self._send(200, json.dumps({"count": len(elements), "elements": elements}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                except Exception as exc:
                    self._send(502, str(exc).encode(), "text/plain; charset=utf-8")
                return
            self._send(404, b"Not found", "text/plain")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/crop":
                self._send(404, b"Not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= _MAX_CROP_REQUEST_BYTES:
                    raise ValueError("crop request is empty or too large")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("crop request must be a JSON object")
                source = frozen_snapshot(payload.get("snapshot_id"))
                if source is None:
                    self._send(404, b"frozen Inspector screenshot was not found or has expired", "text/plain; charset=utf-8")
                    return
                rect = payload.get("rect")
                if not isinstance(rect, dict):
                    raise ValueError("crop request must include a rect object")
                values = tuple(rect.get(name) for name in ("left", "top", "right", "bottom"))
                if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                    raise ValueError("crop rectangle must use integer physical pixels")
                left, top, right, bottom = values
                # Cropping decodes the whole PNG. Serialize this memory-intensive
                # path so concurrent browser tabs cannot multiply peak memory use.
                with crop_lock:
                    image = client.crop_png(source["image"], left, top, right, bottom)
                    width, height = right - left, bottom - top
                    source_width, source_height = int(source["width"]), int(source["height"])
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    stem = f"inspect_crop_{stamp}_x{left}_y{top}_w{width}_h{height}"
                    destination = crop_directory / f"{stem}.png"
                    metadata_destination = crop_directory / f"{stem}.json"
                    metadata = {
                        "source_snapshot_id": str(payload["snapshot_id"]),
                        "source_size": {"width": source_width, "height": source_height},
                        "region": {"left": left, "top": top, "right": right, "bottom": bottom, "width": width, "height": height, "center": {"x": left + width / 2, "y": top + height / 2}},
                        "region_relative": {"left": left / source_width, "top": top / source_height, "right": right / source_width, "bottom": bottom / source_height},
                        "snapshot": source["metadata"],
                        "created_at": now_iso(),
                    }
                    destination.write_bytes(image)
                    metadata_destination.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self._send(200, json.dumps({"path": str(destination), "metadata_path": str(metadata_destination), "metadata": metadata}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            except Exception as exc:
                self._send(400, str(exc).encode(), "text/plain; charset=utf-8")

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{server.server_port}/"
    if open_browser:
        webbrowser.open(url)
    return server


def run_forever(client: "AScriptClient", *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> str:
    server = serve(client, host=host, port=port, open_browser=open_browser)
    url = f"http://{host}:{server.server_port}/"
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
