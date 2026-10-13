'use strict';
const $=id=>document.getElementById(id), token=document.querySelector('meta[name="film-token"]').content;
const state={id:null,info:null,original:null,result:null,mask:null,overlay:null,view:'compare',tool:'pan',scale:1,ox:0,oy:0,split:.5,busy:false,pointer:null,space:false,compareBefore:false,cloneSource:null,reviewIndex:0,reviewGroups:[],reviewPoint:null,ready:false,custom:false,lossyConfirmedFor:null,outdated:false,fitted:true,viewportW:0,viewportH:0};
const stage=$('stage'),canvas=$('viewer'),ctx=canvas.getContext('2d');let toastTimer;
function toast(message,error=false){$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,error?8000:3500);}
function interactive(){
 const loaded=!!state.id,editable=loaded&&!state.busy,ready=editable&&state.ready;
 for(const id of ['exportBtn','originalBtn','changesBtn'])$(id).disabled=!ready;
 for(const id of ['level','correctBtn','panTool','addTool','rejectTool','eraseTool','cloneTool','resampleBtn','zoomOut','zoomIn','fitBtn','actualBtn','settingsBtn','clearBtn','brush','reviewBtn','closeCorrectionBtn','retryBtn','manualBtn'])$(id).disabled=!editable;
 for(const id of ['importBtn','chooseBtn','sampleBtn','applySettingsBtn','sensitivity','size','polarity','protect','hairs','exportOptionsBtn'])$(id).disabled=state.busy;
 if(state.outdated)for(const id of ['importBtn','chooseBtn','sampleBtn'])$(id).disabled=true;
 $('undoBtn').disabled=!editable||!state.info?.undo;
 for(const id of ['keepBtn','restoreBtn','previousBtn','nextBtn'])$(id).disabled=!editable||!state.reviewGroups.length;
}
function busy(on,message='正在自动处理…'){state.busy=on;$('busyOverlay').hidden=!on;$('busyText').textContent=message;interactive();}
async function api(path,body={},raw=false){const r=await fetch(path,{method:'POST',headers:{'X-FilmDust-Token':token,...(raw?{}:{'Content-Type':'application/json'})},body:raw?body:JSON.stringify({id:state.id,...body})});if(!r.ok){let m='操作未完成，请重试。';try{m=(await r.json()).error||m;}catch(_){}throw Error(m);}return r;}
async function loadImage(url){const r=await fetch(url);if(!r.ok)throw Error('预览加载失败，请重试。');return createImageBitmap(await r.blob());}
function imageURL(kind,info=state.info,id=state.id){return `/api/image?id=${encodeURIComponent(id)}&kind=${kind}&v=${info.revision}`;}
function updateInfo(info){
 state.info=info;if(info.detection?.level)$('level').value=info.detection.level;state.custom=!!info.detection?.custom;$('fileName').textContent=info.name;
 $('imageMeta').textContent=`${info.width.toLocaleString()} × ${info.height.toLocaleString()} · ${info.depth} 位 · ${info.profile}`;
 buildReviewGroups();const kept=state.reviewGroups.reduce((n,g)=>n+g.items.length,0);
 $('resultSummary').textContent=info.healing?(info.count||kept?`已处理 ${info.processed_count} 处 · ${kept?`${kept} 处疑点保留原样`:'处理完成'}`:info.detection?.automatic?'未发现需要自动处理的位置':'已恢复原图'):'原图已导入';
 $('keptBar').hidden=!kept;$('keptText').textContent=`${kept} 处疑点已保留原样，可直接保存。`;
 $('reviewBtn').textContent=`按需精修 · ${state.reviewGroups.length} 个区域`;
 const png=$('exportFormat').querySelector('[value="png"]');png.disabled=info.depth===16;if(png.disabled&&(($('exportFormat').value==='png')||($('exportFormat').value==='jpeg'&&state.lossyConfirmedFor!==state.id)))$('exportFormat').value='tiff';updateExportNote();interactive();
}
function buildReviewGroups(){
 const groups=new Map(),cell=Math.max(64,Math.min(240,Math.round(Math.min(state.info.width,state.info.height)/24)));
 for(const item of state.info.reviews||[]){const[x,y]=item.point,key=`${Math.floor(y/cell)}:${Math.floor(x/cell)}`;if(!groups.has(key))groups.set(key,{items:[]});groups.get(key).items.push(item);}
 state.reviewGroups=[...groups.values()];state.reviewIndex=Math.min(state.reviewIndex,Math.max(0,state.reviewGroups.length-1));
 if(!state.reviewGroups.length)$('reviewPanel').hidden=true;
}
async function preview(info,id=state.id){const images=await Promise.all([loadImage(imageURL('result',info,id)),loadImage(imageURL('overlay',info,id))]);if(state.result?.close)state.result.close();if(state.overlay?.close)state.overlay.close();[state.result,state.overlay]=images;}
async function importPhoto(file,sample=false){
 if(state.busy)return;if(file&&file.size>200*1024*1024){toast('单张照片不能超过 200 MB。',true);return;}if(file&&!/\.(jpe?g|png|tiff?)$/i.test(file.name)){toast('请导入 JPEG、PNG 或 TIFF。',true);return;}
 busy(true,'正在导入并自动处理…');$('saveNotice').hidden=true;let uploaded=null;
 try{
  uploaded=await(await api(sample?'/api/sample':`/api/upload?name=${encodeURIComponent(file.name)}`,sample?{}:file,!sample)).json();
  let processed,processingError;
  try{processed=await(await api('/api/process',{id:uploaded.id,level:$('level').value})).json();}catch(e){processingError=e;processed=uploaded;}
  const info={...uploaded,...processed};const original=await loadImage(imageURL('original',info,uploaded.id));
  let result=null,overlay=null;if(!processingError){[result,overlay]=await Promise.all([loadImage(imageURL('result',info,uploaded.id)),loadImage(imageURL('overlay',info,uploaded.id))]);}
  for(const key of ['original','result','overlay'])if(state[key]?.close)state[key].close();
  state.id=uploaded.id;state.lossyConfirmedFor=null;state.original=original;state.result=result;state.overlay=overlay;state.ready=!processingError;state.cloneSource=null;state.reviewIndex=0;state.custom=false;
  updateInfo(info);$('empty').hidden=true;canvas.hidden=false;for(const id of ['workspaceTop','mainControls','workspaceFooter'])$(id).hidden=false;
  $('correctionPanel').hidden=true;$('correctBtn').setAttribute('aria-expanded','false');$('retryBar').hidden=!processingError;
  setTool('pan');fit();setView(processingError?'original':'compare');
  if(processingError)toast(processingError.message,true);else if(info.notes.length)toast(info.notes.join(' '));
 }catch(e){toast(e.message,true);}finally{busy(false);$('fileInput').value='';}
}
function settingsOptions(){return {sensitivity:Number($('sensitivity').value),size:Number($('size').value),polarity:$('polarity').value,protect:$('protect').checked,hairs:$('hairs').checked};}
async function processPhoto(custom=false){
 if(state.busy||!state.id)return;const previousLevel=state.info.detection?.level;busy(true,'正在自动清理并生成对比…');$('saveNotice').hidden=true;
 try{const info=await(await api('/api/process',{level:$('level').value,...(custom?{options:settingsOptions()}:{})})).json();await preview(info);state.ready=true;state.custom=custom;state.cloneSource=null;state.reviewIndex=0;updateInfo({...state.info,...info});$('retryBar').hidden=true;setTool('pan');setView('compare');}catch(e){if(previousLevel)$('level').value=previousLevel;toast(e.message,true);if(!state.ready)$('retryBar').hidden=false;}finally{busy(false);}
}
async function mutation(path,body,message){
 if(state.busy||!state.id)return;const showReviews=!$('reviewPanel').hidden;busy(true,message);$('saveNotice').hidden=true;
 try{await api(path,body);const info=await(await api('/api/heal')).json();await preview(info);state.ready=true;updateInfo({...state.info,...info});setView('compare');if(showReviews&&state.reviewGroups.length)focusReview(false);if(path==='/api/undo')state.cloneSource=null;}catch(e){toast(e.message,true);}finally{busy(false);}
}
function setView(view){state.view=view;$('originalBtn').setAttribute('aria-pressed',String(view==='original'));$('changesBtn').setAttribute('aria-pressed',String(view==='mask'));$('viewBadge').textContent={original:'原图',mask:'改动与保留位置',compare:'前后对比',result:'处理后'}[view];$('viewBadge').hidden=!state.original;$('compareHelp').hidden=view!=='compare';draw();}
function openCorrection(on=true){$('correctionPanel').hidden=!on;$('correctBtn').setAttribute('aria-expanded',String(on));if(!on){$('reviewPanel').hidden=true;setTool('pan');setView('compare');}if(!on)requestAnimationFrame(fit);}
function setTool(tool){state.tool=tool;if(['clone','erase'].includes(tool))document.querySelector('.more-tools').open=true;for(const name of ['pan','add','reject','erase','clone']){$(`${name}Tool`).classList.toggle('active',name===tool);$(`${name}Tool`).setAttribute('aria-pressed',String(name===tool));}canvas.style.cursor=tool==='pan'?'grab':'crosshair';$('brushControl').hidden=tool==='pan';$('hint').textContent={pan:'拖动移动照片，使用缩放按钮查看细节。',add:'涂抹遗漏的灰尘，松开后自动更新对比。',reject:'点击已处理或保留的标记，取消该处；原始像素会恢复。',erase:'涂抹擦除部分标记，松开后自动更新。',clone:'先点击干净位置取样，再涂抹。再次取样可点击「重新取样」。'}[tool];$('resampleBtn').hidden=tool!=='clone';if(tool!=='pan')setView('mask');}
function visiblePhotoCentre(){
 let height=stage.clientHeight;
 const panel=$('correctionPanel');
 if(!panel.hidden&&getComputedStyle(panel).position==='fixed')height=Math.max(64,Math.min(height,panel.getBoundingClientRect().top-stage.getBoundingClientRect().top-12));
 return [stage.clientWidth/2,height/2];
}
function focusReview(open=true){
 if(!state.reviewGroups.length)return;if(open){openCorrection(true);$('reviewPanel').hidden=false;}
 const g=state.reviewGroups[state.reviewIndex];const point=g.items[Math.floor(g.items.length/2)].point;state.reviewPoint=point;
 state.cloneSource=null;setTool('pan');setView('compare');state.fitted=false;state.viewportW=stage.clientWidth;state.viewportH=stage.clientHeight;state.scale=1.5;const[cx,cy]=visiblePhotoCentre();state.ox=cx-point[0]*state.scale;state.oy=cy-point[1]*state.scale;
 $('reviewPosition').textContent=`保留区域 ${state.reviewIndex+1} / ${state.reviewGroups.length}`;$('reviewReason').textContent=`此区域 ${g.items.length} 处 · ${g.items.some(p=>p.kind==='repair')?'边缘无法可靠补齐':'靠近结构、纹理或形态有疑问'} · 原样保留`;draw();
}
function updateExportNote(){const f=$('exportFormat').value;$('exportNote').textContent=f==='jpeg'?(state.info?.depth===16?'JPEG 会转为 8 位并重新压缩全图。':'JPEG 会重新压缩全图。'):'无损保存，保留原始尺寸与色彩配置。';}
const nativeExports=new Map();let nativeExportSerial=0;
window.filmDustNativeExportComplete=message=>{
 const pending=nativeExports.get(message.requestId);if(!pending)return;
 nativeExports.delete(message.requestId);clearTimeout(pending.timer);
 if(message.cancelled)pending.resolve(null);else if(message.error)pending.reject(Error(message.error));else pending.resolve(message.path);
};
function nativeSave(format,name){
 return new Promise((resolve,reject)=>{
  const requestId=String(++nativeExportSerial);
  const timer=setTimeout(()=>{nativeExports.delete(requestId);reject(Error('保存尚未完成，请关闭保存窗口后重试。'));},300000);
  nativeExports.set(requestId,{resolve,reject,timer});
  try{window.webkit.messageHandlers.filmExport.postMessage({requestId,id:state.id,format,name,token});}
  catch(e){clearTimeout(timer);nativeExports.delete(requestId);reject(e);}
 });
}
async function saveCopy(){
 if(state.busy||!state.ready)return;busy(true,'正在生成原尺寸副本…');
 try{const fmt=$('exportFormat').value,name=state.info.name.replace(/\.[^.]+$/,'')+'_除尘'+{tiff:'.tif',png:'.png',jpeg:'.jpg'}[fmt];
  if(window.webkit?.messageHandlers?.filmExport){const path=await nativeSave(fmt,name);if(path){$('saveNotice').textContent=`已保存副本：${path}`;$('saveNotice').hidden=false;}return;}
  const r=await api('/api/export',{format:fmt}),blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);$('saveNotice').textContent=`已生成副本：${name} · 请在浏览器下载位置查看。`;$('saveNotice').hidden=false;
 }catch(e){toast(e.message,true);}finally{busy(false);}
}
function resize(){
 const dpr=window.devicePixelRatio||1,w=stage.clientWidth,h=stage.clientHeight;
 canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);
 if(state.original&&state.fitted){fit();return;}
 if(state.original&&state.viewportW){state.ox+=(w-state.viewportW)/2;state.oy+=(h-state.viewportH)/2;}
 state.viewportW=w;state.viewportH=h;draw();
}
function fit() {
  if(!state.original)return;state.fitted=true;state.viewportW=stage.clientWidth;state.viewportH=stage.clientHeight;
  state.scale=Math.min((stage.clientWidth-54)/state.original.width,(stage.clientHeight-44)/state.original.height,1);
  state.ox=(stage.clientWidth-state.original.width*state.scale)/2;
  state.oy=(stage.clientHeight-state.original.height*state.scale)/2;
  draw();
}
function zoom(scale,cx=stage.clientWidth/2,cy=stage.clientHeight/2) {
  if(!state.original)return;state.fitted=false;
  const min=Math.min(.025,(stage.clientWidth-54)/state.original.width/2);
  const next=Math.max(min,Math.min(8,scale)),ratio=next/state.scale;
  state.ox=cx-(cx-state.ox)*ratio;state.oy=cy-(cy-state.oy)*ratio;state.scale=next;draw();
}
function drawImage(im) {ctx.drawImage(im,state.ox,state.oy,im.width*state.scale,im.height*state.scale);}
function draw() {
  const dpr=window.devicePixelRatio||1,w=stage.clientWidth,h=stage.clientHeight;
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
  if(!state.original)return;
  ctx.imageSmoothingEnabled=state.scale<1;
  const view=state.compareBefore?'original':state.view;
  if(view==='result' && state.result) drawImage(state.result);
  else if(view==='compare' && state.result) {
    drawImage(state.result);ctx.save();ctx.beginPath();ctx.rect(0,0,w*state.split,h);ctx.clip();drawImage(state.original);ctx.restore();
    const x=w*state.split;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.strokeStyle='rgba(255,255,255,.85)';ctx.lineWidth=1;ctx.stroke();
    ctx.save();ctx.shadowColor='rgba(0,0,0,.2)';ctx.shadowBlur=10;ctx.shadowOffsetY=2;ctx.beginPath();ctx.arc(x,h/2,17,0,Math.PI*2);ctx.fillStyle='rgba(255,255,255,.94)';ctx.fill();ctx.restore();ctx.fillStyle='#007aff';ctx.font='16px -apple-system, sans-serif';ctx.textAlign='center';ctx.fillText('↔',x,h/2+5);
  } else {
    drawImage(state.original);
    if(view==='mask' && state.overlay)drawImage(state.overlay);
  }
  if(state.pointer && ['add','clone'].includes(state.pointer.mode)) {
    ctx.save();ctx.translate(state.ox,state.oy);ctx.scale(state.scale,state.scale);ctx.strokeStyle='#f45b429e';ctx.fillStyle='#f45b429e';ctx.lineWidth=Number($('brush').value);ctx.lineCap='round';ctx.lineJoin='round';
    const pts=state.pointer.points;ctx.beginPath();ctx.moveTo(...pts[0]);for(const p of pts.slice(1))ctx.lineTo(...p);ctx.stroke();
    if(pts.length===1){ctx.beginPath();ctx.arc(...pts[0],ctx.lineWidth/2,0,Math.PI*2);ctx.fill();}ctx.restore();
  }
  if(state.tool==='clone' && state.cloneSource){
    const [sx,sy]=state.cloneSource,x=state.ox+sx*state.scale,y=state.oy+sy*state.scale;
    ctx.strokeStyle='#f2b777';ctx.lineWidth=1;ctx.beginPath();ctx.arc(x,y,8,0,Math.PI*2);ctx.moveTo(x-13,y);ctx.lineTo(x+13,y);ctx.moveTo(x,y-13);ctx.lineTo(x,y+13);ctx.stroke();
  }
  if(state.reviewPoint&&!$('reviewPanel').hidden){const x=state.ox+state.reviewPoint[0]*state.scale,y=state.oy+state.reviewPoint[1]*state.scale;ctx.strokeStyle='#efb16b';ctx.lineWidth=1.5;ctx.strokeRect(x-16,y-16,32,32);}
  $('zoomLabel').textContent=`${Math.round(state.scale*100)}%`;
}

function relative(e) {const r=canvas.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top];}
function photoPoint(e) {const[x,y]=relative(e);return[(x-state.ox)/state.scale,(y-state.oy)/state.scale];}
canvas.addEventListener('pointerdown',e=>{
  if(state.busy||!state.id||e.button!==0)return;
  canvas.focus();const[x,y]=relative(e);
  const mode=state.space?'pan':state.tool==='pan'&&state.view==='compare'&&Math.abs(x-stage.clientWidth*state.split)<25?'split':state.tool;
  if(mode!=='pan'&&mode!=='split'){
    const[p,q]=photoPoint(e);if(p<0||q<0||p>=state.info.width||q>=state.info.height)return;setView('mask');
    if(mode==='clone' && (e.altKey || !state.cloneSource)){state.cloneSource=[p,q];toast('已取样。请对齐明暗边缘，再涂抹灰尘；Option/Alt 点击可重新取样。');draw();return;}
  }
  state.pointer={mode,x,y,ox:state.ox,oy:state.oy,points:[photoPoint(e)],pointerId:e.pointerId};
  canvas.setPointerCapture(e.pointerId);if(mode==='pan')canvas.style.cursor='grabbing';draw();
});
canvas.addEventListener('pointermove',e=>{
  if(!state.pointer||state.pointer.pointerId!==e.pointerId)return;
  const p=state.pointer,[x,y]=relative(e);
  if(p.mode==='pan'){state.fitted=false;state.ox=p.ox+x-p.x;state.oy=p.oy+y-p.y;}
  else if(p.mode==='split')state.split=Math.max(.02,Math.min(.98,x/stage.clientWidth));
  else if(p.mode!=='reject')p.points.push(photoPoint(e));
  draw();
});
async function finishPointer(e,cancel=false) {
  const p=state.pointer;if(!p||p.pointerId!==e.pointerId)return;
  state.pointer=null;canvas.style.cursor=state.tool==='pan'?'grab':'crosshair';draw();
  if(cancel||p.mode==='pan'||p.mode==='split')return;
  if(p.mode==='clone')await mutation('/api/clone',{source:state.cloneSource,points:p.points,diameter:Number($('brush').value)},'正在取样修补…');
  else await mutation('/api/edit',{mode:p.mode,points:p.points,diameter:Number($('brush').value)},'正在更新选区…');
}
canvas.addEventListener('pointerup',e=>finishPointer(e));canvas.addEventListener('pointercancel',e=>finishPointer(e,true));
canvas.addEventListener('wheel',e=>{if(!state.id||state.busy)return;e.preventDefault();const[x,y]=relative(e);zoom(state.scale*Math.exp(-e.deltaY*.0015),x,y);},{passive:false});

for(const tool of ['pan','add','reject','erase','clone'])$(`${tool}Tool`).onclick=()=>setTool(tool);
$('resampleBtn').onclick=()=>{state.cloneSource=null;toast('点击干净位置重新取样，再涂抹修补。');draw();};
for(const id of ['importBtn','chooseBtn'])$(id).onclick=()=>$('fileInput').click();
$('fileInput').onchange=e=>{if(e.target.files[0])importPhoto(e.target.files[0]);};$('sampleBtn').onclick=()=>importPhoto(null,true);
$('level').onchange=()=>processPhoto();$('retryBtn').onclick=()=>processPhoto(state.custom);$('manualBtn').onclick=()=>{state.ready=true;$('retryBar').hidden=true;openCorrection(true);interactive();};
$('originalBtn').onclick=()=>setView(state.view==='original'?'compare':'original');$('changesBtn').onclick=()=>setView(state.view==='mask'?'compare':'mask');
$('correctBtn').onclick=()=>openCorrection($('correctionPanel').hidden);$('closeCorrectionBtn').onclick=()=>openCorrection(false);
$('undoBtn').onclick=()=>mutation('/api/undo',{},'正在撤销并更新效果…');$('clearBtn').onclick=()=>{$('settingsDialog').close();mutation('/api/clear',{},'正在恢复原图…');};
$('reviewBtn').onclick=()=>{focusReview();requestAnimationFrame(()=>focusReview(false));};$('nextBtn').onclick=()=>{state.reviewIndex=(state.reviewIndex+1)%state.reviewGroups.length;focusReview(false);};$('previousBtn').onclick=()=>{state.reviewIndex=(state.reviewIndex-1+state.reviewGroups.length)%state.reviewGroups.length;focusReview(false);};
$('keepBtn').onclick=()=>{const g=state.reviewGroups[state.reviewIndex];if(g)mutation('/api/keep',{points:g.items.map(i=>i.point)},'正在保留本区域原样…');};
$('restoreBtn').onclick=()=>{if(state.reviewPoint)mutation('/api/keep',{points:[state.reviewPoint]},'正在还原当前标记…');};
$('zoomIn').onclick=()=>zoom(state.scale*1.35);$('zoomOut').onclick=()=>zoom(state.scale/1.35);$('fitBtn').onclick=fit;$('actualBtn').onclick=()=>zoom(1);
$('settingsBtn').onclick=()=>{const values=state.info.detection?.settings||{sensitivity:{gentle:40,standard:50,more:65}[$('level').value],size:30,polarity:'light',protect:true,hairs:true};for(const id of ['sensitivity','size']){$(id).value=values[id];$(`${id}Val`).textContent=values[id];}$('polarity').value=values.polarity;$('protect').checked=values.protect;$('hairs').checked=values.hairs;$('settingsDialog').showModal();};
$('applySettingsBtn').onclick=()=>{$('settingsDialog').close();processPhoto(true);};
for(const id of ['sensitivity','size','brush'])$(id).oninput=()=>{$(`${id}Val`).textContent=$(id).value;};
$('exportBtn').onclick=saveCopy;$('exportOptionsBtn').onclick=()=>$('exportDialog').showModal();$('exportFormat').onchange=()=>{state.lossyConfirmedFor=$('exportFormat').value==='jpeg'&&state.info?.depth===16?state.id:null;updateExportNote();};$('helpBtn').onclick=()=>$('helpDialog').showModal();
let dragDepth=0;document.addEventListener('dragenter',e=>{e.preventDefault();if(e.dataTransfer.types.includes('Files')){dragDepth++;$('dropOverlay').hidden=false;}});document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('dragleave',e=>{e.preventDefault();dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)$('dropOverlay').hidden=true;});document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('dropOverlay').hidden=true;if(e.dataTransfer.files[0])importPhoto(e.dataTransfer.files[0]);});
document.addEventListener('keydown',e=>{if(['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)||document.querySelector('dialog[open]')||!state.id||state.busy)return;if(e.key===' '){e.preventDefault();state.space=true;canvas.style.cursor='grab';return;}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='z'){e.preventDefault();if(state.info.undo)mutation('/api/undo',{},'正在撤销并更新…');return;}const key=e.key.toLowerCase(),tool={h:'pan',b:'add',e:'reject',r:'erase',s:'clone'}[key];if(tool){openCorrection(true);setTool(tool);}if(key==='o'){state.compareBefore=true;draw();}});
document.addEventListener('keyup',e=>{if(e.key===' '){state.space=false;canvas.style.cursor=state.tool==='pan'?'grab':'crosshair';}if(e.key.toLowerCase()==='o'){state.compareBefore=false;draw();}});window.addEventListener('blur',()=>{state.space=false;state.compareBefore=false;draw();});
new ResizeObserver(resize).observe(stage);resize();interactive();

fetch('/api/health').then(r=>r.json()).then(info=>{if(!info.automatic){state.outdated=true;document.querySelector('#empty h2').textContent='请从新版启动窗口打开';document.querySelector('#empty p').textContent='此窗口连接的是旧服务。重新双击启动 FilmDust.command，并打开它显示的新地址。';interactive();}}).catch(()=>toast('本地服务未连接，请检查启动窗口。',true));
