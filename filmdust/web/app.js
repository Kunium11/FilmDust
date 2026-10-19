'use strict';
const $=id=>document.getElementById(id), token=document.querySelector('meta[name="film-token"]').content;
const state={id:null,info:null,original:null,result:null,mask:null,overlay:null,view:'compare',tool:'pan',scale:1,ox:0,oy:0,split:.5,busy:false,pointer:null,space:false,compareBefore:false,cloneSource:null,reviewIndex:0,reviewGroups:[],reviewPoint:null,ready:false,custom:false,lossyConfirmedFor:null,outdated:false,fitted:true,viewportW:0,viewportH:0,savedRevision:null,renderedRevision:null,pendingPreview:false,lastDuration:null,lastEditDuration:null,fullResolution:false,previewEpoch:0,sharpening:false};
const quickEdge=2048;
const stage=$('stage'),canvas=$('viewer'),ctx=canvas.getContext('2d');let toastTimer;
function toast(message,error=false){$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,error?8000:3500);}
const defaults={sensitivity:70,size:30,polarity:'light',protect:true,hairs:true};
function normalizePreferences(value={}){
 return {sensitivity:Math.max(1,Math.min(100,Number(value.sensitivity)||defaults.sensitivity)),size:Math.max(4,Math.min(100,Number(value.size)||defaults.size)),polarity:['light','dark','both'].includes(value.polarity)?value.polarity:defaults.polarity,protect:typeof value.protect==='boolean'?value.protect:true,hairs:typeof value.hairs==='boolean'?value.hairs:true};
}
let preferences;
try{preferences=normalizePreferences(window.filmDustPreferences||JSON.parse(localStorage.getItem('filmdust.preferences.v1')||'{}'));}catch(_){preferences={...defaults};}
function storePreferences(){
 try{localStorage.setItem('filmdust.preferences.v1',JSON.stringify(preferences));}catch(_){}
 window.webkit?.messageHandlers?.filmPreferences?.postMessage(preferences);
}
function interactive(){
 const loaded=!!state.id,editable=loaded&&!state.busy&&state.ready;
 for(const id of ['exportBtn','originalBtn','changesBtn','correctBtn','panTool','addTool','rejectTool','eraseTool','cloneTool','resampleBtn','clearBtn','brush','reviewBtn','closeCorrectionBtn'])$(id).disabled=!editable;
 for(const id of ['zoomOut','zoomIn','fitBtn','actualBtn'])$(id).disabled=!loaded||state.busy;
 for(const id of ['importBtn','chooseBtn','sampleBtn','applySettingsBtn','sensitivity','size','polarity','protect','hairs','exportOptionsBtn','settingsBtn'])$(id).disabled=state.busy;
 $('retryBtn').disabled=state.busy;
 if(state.outdated)for(const id of ['importBtn','chooseBtn','sampleBtn'])$(id).disabled=true;
 $('undoBtn').disabled=!editable||!state.info?.undo;
 for(const id of ['keepBtn','restoreBtn','previousBtn','nextBtn'])$(id).disabled=!editable||!state.reviewGroups.length;
 $('exportBtn').title=state.pendingPreview?'请先重新加载预览':'保存当前显示结果的副本';
}
let busyTimer,busyStarted;
function busy(on,message='正在自动处理…',local=false){
 state.busy=on;$('busyOverlay').hidden=!on||local;$('busyText').textContent=message;
 if(state.sharpening && !on){$('editStatus').hidden=false;$('editStatus').textContent='正在载入完整清晰度…';}
 else{$('editStatus').hidden=!on||!local;if(on)$('editStatus').textContent=message;}
 clearInterval(busyTimer);
 if(on){busyStarted=performance.now();$('busyElapsed').textContent='照片留在本机处理';busyTimer=setInterval(()=>$('busyElapsed').textContent=`已用时 ${((performance.now()-busyStarted)/1000).toFixed(1)} 秒`,100);}
 interactive();
}
async function api(path,body={},raw=false){const r=await fetch(path,{method:'POST',headers:{'X-FilmDust-Token':token,...(raw?{}:{'Content-Type':'application/json'})},body:raw?body:JSON.stringify({id:state.id,...body})});if(!r.ok){let m='操作未完成，请重试。';try{m=(await r.json()).error||m;}catch(_){}throw Error(m);}return r;}
async function loadImage(url){const r=await fetch(url);if(!r.ok)throw Error('预览加载失败，请重新加载。');return createImageBitmap(await r.blob());}
async function loadImages(urls){
 const items=await Promise.allSettled(urls.map(loadImage));
 const failed=items.find(item=>item.status==='rejected');
 if(failed){for(const item of items)if(item.status==='fulfilled')item.value.close();throw failed.reason;}
 return items.map(item=>item.value);
}
function afterPreviewDraw(){
 // WKWebView can suspend animation frames when its window is covered. The
 // preview is already decoded and drawn; never leave controls locked on a frame.
 return new Promise(resolve=>{
  let frame;
  const finish=()=>{clearTimeout(timer);if(frame!==undefined)cancelAnimationFrame(frame);resolve();};
  const timer=setTimeout(finish,150);
  frame=requestAnimationFrame(()=>{frame=requestAnimationFrame(finish);});
 });
}
function imageURL(kind,info=state.info,id=state.id,maxEdge=0){
 let url=`/api/image?id=${encodeURIComponent(id)}&kind=${kind}&v=${info.revision}`;
 if(maxEdge)url+=`&max=${maxEdge}`;
 return url;
}
function progressive(info){return Math.max(info.width,info.height)>quickEdge;}
function photoWidth(){return state.info?.width||state.original?.width||1;}
function photoHeight(){return state.info?.height||state.original?.height||1;}
function updateInfo(info){
 state.info=info;$('fileName').textContent=info.name;
 $('imageMeta').textContent=`${info.width.toLocaleString()} × ${info.height.toLocaleString()} · ${info.depth} 位 · ${info.profile}`;
 buildReviewGroups();const kept=(info.reviews||[]).length;
 const duration=state.lastEditDuration!==null?` · 本次修正 ${state.lastEditDuration.toFixed(1)} 秒`:state.lastDuration===null?'':` · ${state.lastDuration.toFixed(1)} 秒`;
 $('resultSummary').textContent=info.healing?(info.count||kept?`已处理 ${info.processed_count} 处${duration}${kept?` · ${kept} 处保留原样`:''}`:info.detection?.automatic?`未发现需要自动处理的位置${duration}`:'已恢复原图'):'原图已导入';
 $('keptBar').hidden=!state.reviewGroups.length;$('keptText').textContent=kept?`红色是改动，黄色是保留的疑点。可直接保存，也可按需检查。`:'红色是已处理位置。误修可点击还原。';
 $('reviewBtn').textContent=`检查改动 · ${state.reviewGroups.length} 个区域`;
 const png=$('exportFormat').querySelector('[value="png"]');png.disabled=info.depth===16;
 if(png.disabled&&(($('exportFormat').value==='png')||($('exportFormat').value==='jpeg'&&state.lossyConfirmedFor!==state.id)))$('exportFormat').value='tiff';
 updateExportNote();interactive();
}
function buildReviewGroups(){
 const groups=new Map(),cell=Math.max(64,Math.min(240,Math.round(Math.min(state.info.width,state.info.height)/24)));
 for(const item of [...(state.info.changes||[]),...(state.info.reviews||[])]){const[x,y]=item.point,key=`${Math.floor(y/cell)}:${Math.floor(x/cell)}`;if(!groups.has(key))groups.set(key,{items:[]});groups.get(key).items.push(item);}
 state.reviewGroups=[...groups.values()];state.reviewIndex=Math.min(state.reviewIndex,Math.max(0,state.reviewGroups.length-1));
 if(!state.reviewGroups.length)$('reviewPanel').hidden=true;
}
function releaseImage(im){if(im?.close)im.close();else if(im?.getContext){im.width=1;im.height=1;}}
function mutableImage(im){
 if(im.getContext)return im;
 const surface=document.createElement('canvas');surface.width=im.width;surface.height=im.height;
 surface.getContext('2d').drawImage(im,0,0);return surface;
}
function showPhoto(info){
 $('empty').hidden=true;canvas.hidden=false;
 for(const id of ['workspaceTop','mainControls','workspaceFooter'])$(id).hidden=false;
 $('fileName').textContent=info.name;
 $('imageMeta').textContent=`${info.width.toLocaleString()} × ${info.height.toLocaleString()} · ${info.depth} 位 · ${info.profile}`;
}
async function loadFullPreview(info,id,epoch){
 state.sharpening=true;$('editStatus').hidden=false;$('editStatus').textContent='正在载入完整清晰度…';
 let images=[];
 try{
  images=await loadImages(['original','result','overlay'].map(kind=>imageURL(kind,info,id)));
  if(epoch!==state.previewEpoch||state.id!==id||state.renderedRevision!==info.revision){for(const im of images)im.close();return;}
  const previous={original:state.original,result:state.result,overlay:state.overlay};
  [state.original,state.result,state.overlay]=images;images=[];
  for(const key of ['original','result','overlay'])if(previous[key]&&previous[key]!==state[key])releaseImage(previous[key]);
  state.fullResolution=true;draw();
 }catch(e){
  for(const im of images)im.close();
  if(epoch===state.previewEpoch)toast(`${e.message} 当前仍可比较低清晰度结果。`,true);
 }finally{
  if(epoch===state.previewEpoch){state.sharpening=false;if(!state.busy)$('editStatus').hidden=true;}
 }
}
async function preview(info,id=state.id){
 const epoch=++state.previewEpoch;
 const patch=info.preview_patch,rect=patch?.bounds;
 const local=state.fullResolution&&patch&&patch.from_revision===state.renderedRevision&&state.result&&state.overlay&&rect&&rect[2]*rect[3]<=2_000_000;
 if(local&&rect[2]&&rect[3]){
  const images=await loadImages(['result','overlay'].map(kind=>imageURL(kind,info,id)+`&rect=${rect.join(',')}`));
  if(epoch!==state.previewEpoch){for(const im of images)im.close();return;}
  try{
   const result=mutableImage(state.result),overlay=mutableImage(state.overlay);
   result.getContext('2d').drawImage(images[0],rect[0],rect[1]);
   const c=overlay.getContext('2d');c.clearRect(...rect);c.drawImage(images[1],rect[0],rect[1]);
   if(result!==state.result)releaseImage(state.result);if(overlay!==state.overlay)releaseImage(state.overlay);
   state.result=result;state.overlay=overlay;
  }finally{for(const im of images)im.close();}
  state.sharpening=false;if(!state.busy)$('editStatus').hidden=true;
 }else if(!local){
  const maxEdge=progressive(info)?quickEdge:0;
  const images=await loadImages(['result','overlay'].map(kind=>imageURL(kind,info,id,maxEdge)));
  if(epoch!==state.previewEpoch||state.id!==id){for(const im of images)im.close();return;}
  for(const key of ['result','overlay'])releaseImage(state[key]);
  [state.result,state.overlay]=images;
  state.fullResolution=!progressive(info);
 }
 state.renderedRevision=info.revision;state.pendingPreview=false;state.ready=true;$('retryBar').hidden=true;
 if(!local&&progressive(info))loadFullPreview(info,id,epoch);
}
function needsSave(){return !!state.id&&state.info.revision>0&&state.savedRevision!==state.info.revision;}
async function confirmLeave(){
 if(state.busy){toast('请等当前处理或保存完成。');return false;}
 if(!needsSave())return true;
 const dialog=$('unsavedDialog');if(dialog.open)return false;
 $('unsavedMessage').textContent=`“${state.info.name}”的处理结果还没有保存副本。`;
 $('saveAndContinue').disabled=!state.ready;
 const choice=await new Promise(resolve=>{dialog.returnValue='cancel';dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true});dialog.showModal();});
 if(choice==='discard')return true;
 if(choice==='save')return await saveCopy();
 return false;
}
window.filmDustRequestClose=async()=>{if(await confirmLeave())window.webkit?.messageHandlers?.filmClose?.postMessage({confirmed:true});};
window.addEventListener('beforeunload',e=>{if(needsSave()||state.busy){e.preventDefault();e.returnValue='';}});
async function importPhoto(file,sample=false){
 if(state.busy)return;
 if(file&&file.size>200*1024*1024){toast('单张照片不能超过 200 MB。',true);return;}
 if(file&&!/\.(jpe?g|png|tiff?)$/i.test(file.name)){toast('请导入 JPEG、PNG 或 TIFF。',true);return;}
 if(!await confirmLeave()){$('fileInput').value='';return;}
 const started=performance.now();busy(true,'1 / 3 · 正在读取照片…');let uploaded=null,images=[],revealed=false,committed=false;
 const epoch=++state.previewEpoch;
 try{
  const retain=encodeURIComponent(state.id||'');
  uploaded=await(await api(sample?`/api/sample?retain=${retain}`:`/api/upload?name=${encodeURIComponent(file.name)}&retain=${retain}`,sample?{}:file,!sample)).json();
  if(epoch!==state.previewEpoch)throw Error('导入已取消。');
  const large=progressive(uploaded),showEarly=large&&!state.id;
  $('busyText').textContent='2 / 3 · 正在识别并去除灰尘…';
  const processedPromise=api('/api/process',{id:uploaded.id,level:'auto',options:preferences}).then(r=>r.json());
  if(showEarly){
   loadImage(imageURL('original',uploaded,uploaded.id,quickEdge)).then(image=>{
    if(committed||epoch!==state.previewEpoch||state.renderedRevision!=null){image.close();return;}
    const previous=state.original;
    state.id=uploaded.id;state.info=uploaded;state.original=image;state.result=null;state.overlay=null;
    state.ready=false;state.fullResolution=false;state.renderedRevision=null;
    if(previous&&previous!==image)releaseImage(previous);
    showPhoto(uploaded);$('resultSummary').textContent='正在识别并去除灰尘…';
    $('correctionPanel').hidden=true;$('retryBar').hidden=true;revealed=true;
    busy(true,'2 / 3 · 正在识别并去除灰尘…',true);setView('original');fit();
   }).catch(()=>{});
  }
  const processed=await processedPromise;
  if(epoch!==state.previewEpoch)throw Error('导入已取消。');
  const info={...uploaded,...processed};
  if(revealed)busy(true,'3 / 3 · 正在生成对比…',true);
  else $('busyText').textContent='3 / 3 · 正在生成对比…';
  const maxEdge=large?quickEdge:0;
  images=await loadImages(['original','result','overlay'].map(kind=>imageURL(kind,info,uploaded.id,maxEdge)));
  if(epoch!==state.previewEpoch){for(const im of images)im.close();images=[];throw Error('导入已取消。');}
  await api('/api/activate',{id:uploaded.id});
  const previous=revealed?null:state.id;
  const prior={original:state.original,result:state.result,overlay:state.overlay};
  state.id=uploaded.id;state.lossyConfirmedFor=null;[state.original,state.result,state.overlay]=images;images=[];
  for(const key of ['original','result','overlay'])if(prior[key]&&prior[key]!==state[key])releaseImage(prior[key]);
  state.ready=true;state.pendingPreview=false;state.fullResolution=!large;state.renderedRevision=info.revision;state.savedRevision=null;state.cloneSource=null;state.reviewIndex=0;state.lastDuration=null;state.lastEditDuration=null;
  $('saveNotice').hidden=true;updateInfo(info);showPhoto(info);
  $('correctionPanel').hidden=true;$('correctBtn').setAttribute('aria-expanded','false');$('retryBar').hidden=true;
  setTool('pan');fit();setView('compare');
  await afterPreviewDraw();
  state.lastDuration=(performance.now()-started)/1000;updateInfo(info);
  committed=true;
  if(previous)api('/api/release',{id:previous}).catch(()=>{});
  if(info.notes.length)toast(info.notes.join(' '));
  if(large)loadFullPreview(info,uploaded.id,epoch);
 }catch(e){
  state.previewEpoch++;
  for(const im of images)im.close();
  if(revealed&&!committed){
   for(const key of ['original','result','overlay'])releaseImage(state[key]);
   state.id=null;state.info=null;state.original=null;state.result=null;state.overlay=null;state.ready=false;state.renderedRevision=null;state.fullResolution=false;
   $('empty').hidden=false;canvas.hidden=true;
   for(const id of ['workspaceTop','mainControls','workspaceFooter'])$(id).hidden=true;
   $('viewBadge').hidden=true;$('compareHelp').hidden=true;$('compareSlider').hidden=true;
  }
  if(uploaded&&uploaded.id!==state.id)api('/api/release',{id:uploaded.id}).catch(()=>{});
  toast(`${e.message}${state.id?' 当前照片仍可继续处理。':''}`,true);
 }
 finally{busy(false);$('fileInput').value='';}
}
function settingsOptions(){return normalizePreferences({sensitivity:Number($('sensitivity').value),size:Number($('size').value),polarity:$('polarity').value,protect:$('protect').checked,hairs:$('hairs').checked});}
function markPreviewUnavailable(){
 state.ready=false;state.pendingPreview=true;$('retryBar').hidden=false;
 $('resultSummary').textContent='预览未更新 · 重新加载后可保存';setView('original');interactive();
}
async function synchronizeAfterFailure(){
 try{const info=await(await api('/api/info')).json();updateInfo(info);if(info.revision!==state.renderedRevision)markPreviewUnavailable();}
 catch(_){markPreviewUnavailable();}
}
async function processPhoto(){
 if(state.busy||!state.id)return;busy(true,'正在识别并去除灰尘…',true);$('saveNotice').hidden=true;const started=performance.now();
 try{const info=await(await api('/api/process',{revision:state.info.revision,level:'auto',options:preferences})).json();state.ready=false;state.lastDuration=null;state.lastEditDuration=null;updateInfo(info);await preview(info);state.cloneSource=null;state.reviewIndex=0;setTool('pan');setView('compare');await afterPreviewDraw();state.lastDuration=(performance.now()-started)/1000;updateInfo(info);}
 catch(e){await synchronizeAfterFailure();toast(e.message,true);}finally{busy(false);}
}
async function retryPreview(){
 if(state.busy||!state.id)return;busy(true,'正在重新加载处理结果…');
 try{const info=await(await api('/api/info')).json();await preview(info);updateInfo(info);setView('compare');}
 catch(e){markPreviewUnavailable();toast(e.message,true);}finally{busy(false);}
}
async function mutation(path,body,message){
 if(state.busy||!state.id||!state.ready)return;
 const showReviews=!$('reviewPanel').hidden,started=performance.now();busy(true,message,true);$('saveNotice').hidden=true;
 try{
  const info=await(await api(path,{revision:state.info.revision,...body})).json();state.ready=false;state.lastDuration=null;state.lastEditDuration=null;
  updateInfo(info);await preview(info);setView('compare');
  if(showReviews&&state.reviewGroups.length)focusReview(false);if(path==='/api/undo')state.cloneSource=null;
  await afterPreviewDraw();
  state.lastEditDuration=(performance.now()-started)/1000;updateInfo(info);
  if(path==='/api/edit'&&body.mode==='add'&&info.edit?.changed_pixels===0)toast('这一处没有新增像素变化；请确认画笔覆盖了灰尘，或使用取样修补。');
 }catch(e){await synchronizeAfterFailure();toast(e.message,true);}finally{busy(false);}
}
function setView(view){state.view=view;$('originalBtn').setAttribute('aria-pressed',String(view==='original'));$('changesBtn').setAttribute('aria-pressed',String(view==='mask'));$('viewBadge').textContent={original:'原图',mask:'改动与保留位置',compare:'前后对比',result:'处理后'}[view];$('viewBadge').hidden=!state.original;$('compareHelp').hidden=view!=='compare';$('compareSlider').hidden=view!=='compare';draw();}
function openCorrection(on=true){$('correctionPanel').hidden=!on;$('correctBtn').setAttribute('aria-expanded',String(on));if(!on){$('reviewPanel').hidden=true;setTool('pan');setView('compare');}if(!on)requestAnimationFrame(fit);}
function setTool(tool){state.tool=tool;if(['clone','erase'].includes(tool))document.querySelector('.more-tools').open=true;for(const name of ['pan','add','reject','erase','clone']){$(`${name}Tool`).classList.toggle('active',name===tool);$(`${name}Tool`).setAttribute('aria-pressed',String(name===tool));}canvas.style.cursor=tool==='pan'?'grab':'crosshair';$('brushControl').hidden=tool==='pan';$('hint').textContent={pan:'拖动移动照片，使用缩放按钮查看细节。',add:'覆盖灰尘后松开，只修补这一处。点到已有标记会修补整处；可撤销。',reject:'点击已处理或保留的标记，取消该处；原始像素会恢复。',erase:'涂抹擦除部分标记，松开后自动更新。',clone:'先点击干净位置取样，再涂抹。再次取样可点击「重新取样」。'}[tool];$('resampleBtn').hidden=tool!=='clone';if(tool!=='pan')setView('mask');}
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
 $('reviewPosition').textContent=`检查区域 ${state.reviewIndex+1} / ${state.reviewGroups.length}`;const changed=g.items.filter(i=>i.kind==='processed').length;$('reviewReason').textContent=`此区域 ${changed} 处已处理，${g.items.length-changed} 处保留原样`;$('keepBtn').textContent='还原整个区域';draw();
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
  try{window.webkit.messageHandlers.filmExport.postMessage({requestId,id:state.id,revision:state.info.revision,format,name,token});}
  catch(e){clearTimeout(timer);nativeExports.delete(requestId);reject(e);}
 });
}
async function saveCopy(){
 if(state.busy||!state.ready||state.renderedRevision!==state.info.revision)return false;busy(true,'正在生成原尺寸副本…');
 try{const fmt=$('exportFormat').value,name=state.info.name.replace(/\.[^.]+$/,'')+'_除尘'+{tiff:'.tif',png:'.png',jpeg:'.jpg'}[fmt];
  if(window.webkit?.messageHandlers?.filmExport){const path=await nativeSave(fmt,name);if(path){$('saveNotice').textContent=`已保存副本：${path}`;$('saveNotice').hidden=false;state.savedRevision=state.info.revision;return true;}return false;}
  const r=await api('/api/export',{format:fmt,revision:state.info.revision}),blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);$('saveNotice').textContent=`已生成副本：${name} · 请在浏览器下载位置查看。`;$('saveNotice').hidden=false;state.savedRevision=state.info.revision;return true;
 }catch(e){toast(e.message,true);return false;}finally{busy(false);}
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
  const width=photoWidth(),height=photoHeight();
  state.scale=Math.min((stage.clientWidth-54)/width,(stage.clientHeight-44)/height,1);
  state.ox=(stage.clientWidth-width*state.scale)/2;
  state.oy=(stage.clientHeight-height*state.scale)/2;
  draw();
}
function zoom(scale,cx=stage.clientWidth/2,cy=stage.clientHeight/2) {
  if(!state.original)return;state.fitted=false;
  const min=Math.min(.025,(stage.clientWidth-54)/photoWidth()/2);
  const next=Math.max(min,Math.min(8,scale)),ratio=next/state.scale;
  state.ox=cx-(cx-state.ox)*ratio;state.oy=cy-(cy-state.oy)*ratio;state.scale=next;draw();
}
function drawImage(im) {ctx.drawImage(im,state.ox,state.oy,photoWidth()*state.scale,photoHeight()*state.scale);}
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
  $('compareSlider').value=Math.round(state.split*100);
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
  if(state.busy||!state.id||!state.ready||e.button!==0)return;
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
  else await mutation('/api/edit',{mode:p.mode,points:p.points,diameter:Number($('brush').value)},'正在修补这一处…');
}
canvas.addEventListener('pointerup',e=>finishPointer(e));canvas.addEventListener('pointercancel',e=>finishPointer(e,true));
canvas.addEventListener('wheel',e=>{if(!state.id||state.busy)return;e.preventDefault();const[x,y]=relative(e);zoom(state.scale*Math.exp(-e.deltaY*.0015),x,y);},{passive:false});

for(const tool of ['pan','add','reject','erase','clone'])$(`${tool}Tool`).onclick=()=>setTool(tool);
$('resampleBtn').onclick=()=>{state.cloneSource=null;toast('点击干净位置重新取样，再涂抹修补。');draw();};
for(const id of ['importBtn','chooseBtn'])$(id).onclick=()=>$('fileInput').click();
$('fileInput').onchange=e=>{if(e.target.files[0])importPhoto(e.target.files[0]);};$('sampleBtn').onclick=()=>importPhoto(null,true);
$('retryBtn').onclick=retryPreview;
$('originalBtn').onclick=()=>setView(state.view==='original'?'compare':'original');$('changesBtn').onclick=()=>setView(state.view==='mask'?'compare':'mask');
$('correctBtn').onclick=()=>openCorrection($('correctionPanel').hidden);$('closeCorrectionBtn').onclick=()=>openCorrection(false);
$('undoBtn').onclick=()=>mutation('/api/undo',{},'正在撤销并更新效果…');$('clearBtn').onclick=()=>{$('settingsDialog').close();mutation('/api/clear',{},'正在恢复原图…');};
$('reviewBtn').onclick=()=>{focusReview();requestAnimationFrame(()=>focusReview(false));};$('nextBtn').onclick=()=>{state.reviewIndex=(state.reviewIndex+1)%state.reviewGroups.length;focusReview(false);};$('previousBtn').onclick=()=>{state.reviewIndex=(state.reviewIndex-1+state.reviewGroups.length)%state.reviewGroups.length;focusReview(false);};
$('keepBtn').onclick=()=>{const g=state.reviewGroups[state.reviewIndex];if(g)mutation('/api/keep',{points:g.items.map(i=>i.point)},'正在保留本区域原样…');};
$('restoreBtn').onclick=()=>{if(state.reviewPoint)mutation('/api/keep',{points:[state.reviewPoint]},'正在还原当前标记…');};
$('zoomIn').onclick=()=>zoom(state.scale*1.35);$('zoomOut').onclick=()=>zoom(state.scale/1.35);$('fitBtn').onclick=fit;$('actualBtn').onclick=()=>zoom(1);
function showPreferences(values=preferences){for(const id of ['sensitivity','size']){$(id).value=values[id];$(`${id}Val`).textContent=values[id];}$('polarity').value=values.polarity;$('protect').checked=values.protect;$('hairs').checked=values.hairs;}
$('settingsBtn').onclick=()=>{showPreferences();$('settingsDialog').showModal();};
$('applySettingsBtn').onclick=()=>{preferences=settingsOptions();storePreferences();$('settingsDialog').close();if(state.id)processPhoto();};
$('resetSettingsBtn').onclick=()=>showPreferences(defaults);
for(const id of ['sensitivity','size','brush'])$(id).oninput=()=>{$(`${id}Val`).textContent=$(id).value;};
$('exportBtn').onclick=saveCopy;$('exportOptionsBtn').onclick=()=>$('exportDialog').showModal();$('exportFormat').onchange=()=>{state.lossyConfirmedFor=$('exportFormat').value==='jpeg'&&state.info?.depth===16?state.id:null;updateExportNote();};$('helpBtn').onclick=()=>$('helpDialog').showModal();
let dragDepth=0;document.addEventListener('dragenter',e=>{e.preventDefault();if(e.dataTransfer.types.includes('Files')){dragDepth++;$('dropOverlay').hidden=false;}});document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('dragleave',e=>{e.preventDefault();dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)$('dropOverlay').hidden=true;});document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('dropOverlay').hidden=true;if(e.dataTransfer.files[0])importPhoto(e.dataTransfer.files[0]);});
document.addEventListener('keydown',e=>{if(['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)||document.querySelector('dialog[open]')||!state.id||state.busy)return;if(e.key===' '){e.preventDefault();state.space=true;canvas.style.cursor='grab';return;}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='s'){e.preventDefault();saveCopy();return;}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='z'){e.preventDefault();if(state.info.undo)mutation('/api/undo',{},'正在撤销并更新…');return;}const key=e.key.toLowerCase(),tool={h:'pan',b:'add',e:'reject',r:'erase',s:'clone'}[key];if(tool){openCorrection(true);setTool(tool);}if(key==='o'){state.compareBefore=true;draw();}});
document.addEventListener('keyup',e=>{if(e.key===' '){state.space=false;canvas.style.cursor=state.tool==='pan'?'grab':'crosshair';}if(e.key.toLowerCase()==='o'){state.compareBefore=false;draw();}});window.addEventListener('blur',()=>{state.space=false;state.compareBefore=false;draw();});
$('compareSlider').oninput=e=>{state.split=Number(e.target.value)/100;draw();};
new ResizeObserver(resize).observe(stage);resize();interactive();

fetch('/api/health').then(r=>r.json()).then(info=>{if(!info.automatic){state.outdated=true;document.querySelector('#empty h2').textContent='请从新版启动窗口打开';document.querySelector('#empty p').textContent='此窗口连接的是旧服务。重新双击启动 FilmDust.command，并打开它显示的新地址。';interactive();}}).catch(()=>toast('本地服务未连接，请检查启动窗口。',true));
