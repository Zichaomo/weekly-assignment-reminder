const drafts=new Map();
function openDlg(d){closeAllPops();if(!d.open)d.showModal();icons();}
function formSnapshot(){return ['fCourse','fTitle','fDate','fTime','fNote'].map(id=>$('#'+id).value).join('\u0000');}
let formInitial='',formItem=null,formSaving=false,confirmAction=null;
let recoveredForm=null;
try { const d=JSON.parse(sessionStorage.getItem('homeworkUpdateDraft')||'null'); if(d?.fCourse||d?.fTitle)recoveredForm=d; } catch(e) {}
function canClose(d){
  if(d.id==='dlgForm'&&(formSaving || (formSnapshot()!==formInitial&&!confirm('表单有未保存的修改，放弃修改？'))))return false;
  if(d.id==='dlgCrop'&&(crop.saving || (crop.dirty&&!confirm('裁剪尚未保存，放弃选区修改？'))))return false;
  if(d.id==='dlgReview'&&reviewDirty()&&!confirm('正文草稿尚未保存，关闭后保留本页草稿？'))return false;
  return true;
}
document.querySelectorAll('.dlg').forEach(d=>{
  d.addEventListener('click',e=>{if((e.target===d || e.target.closest('[data-close]'))&&canClose(d))d.close();});
  d.addEventListener('cancel',e=>{if(!canClose(d))e.preventDefault();});
});
function confirmDlg({icon='info',title,body,ok,danger=false,action}){
  $('#cfIcon').innerHTML=`<i data-lucide="${esc(icon)}"></i>`;$('#cfTitle').textContent=title;$('#cfBody').textContent=body;$('#cfOk').textContent=ok;$('#cfOk').className='btn solid'+(danger?' danger':'');confirmAction=action;openDlg($('#dlgConfirm'));
}
$('#cfOk').onclick=guarded(async()=>{
  $('#cfOk').disabled=true;
  try{await confirmAction?.();$('#dlgConfirm').close();}finally{$('#cfOk').disabled=false;}
});
const listInfo={course:{label:'课程黑名单',field:'blacklisted',desc:'整门课的作业和测试均不再检索。'},quiz:{label:'测试黑名单',field:'test_blacklisted',desc:'仅跳过本课在线测试，作业照常检索。'},elective:{label:'选修课名单',field:'elective',desc:'照常检索；后续班级提醒排除此课，个人完整提醒保留。'}};
let cmTab='course';
async function loadCourses(){
  const res=requireOk(await api('/api/courses'));
  const lists=await Promise.all(['course','test','elective'].map(kind=>api('/api/blacklist?kind='+kind)));
  lists.forEach(requireOk);state.courses=res.courses||[];
  for(let i=0;i<3;i++)state.lists[['course','quiz','elective'][i]]=lists[i].blacklist||[];
  $('#courseOptions').innerHTML=state.courses.map(c=>`<option value="${esc(c.name)}">${esc(c.lid)}</option>`).join('');
  renderCourses();
}
function renderCourses(){
  const info=listInfo[cmTab],q=$('#cmQ').value.trim().toLowerCase();
  $('#cmTabs').dataset.i=['course','quiz','elective'].indexOf(cmTab);
  $('#cmTabs').querySelectorAll('button').forEach(b=>{b.setAttribute('aria-selected',String(b.dataset.tab===cmTab));b.tabIndex=b.dataset.tab===cmTab?0:-1;b.querySelector('b').textContent=(state.lists[b.dataset.tab]||[]).length;});
  $('#cmDesc').textContent=info.label+'：'+info.desc;$('#cmAdd').hidden=cmTab!=='elective';
  const joined=state.lists[cmTab]||[],ids=new Set(joined.map(c=>String(c.lid)));
  const match=c=>!q||(c.name+String(c.lid)).toLowerCase().includes(q);
  const avail=state.courses.filter(c=>!ids.has(String(c.lid))).filter(match),members=joined.filter(match);
  const row=(c,joined)=>`<li class="crow"><span class="cn"><span class="name">${esc(c.name||'未命名课程')}</span><span class="sub num">${esc(c.lid)}</span></span><button class="btn sm ${joined?'ghost':'line'}" data-lid="${esc(c.lid)}" data-action="${joined?'remove':'apply'}">${joined?'移出':'加入'}</button></li>`;
  $('#cmAvail').innerHTML=avail.map(c=>row(c,false)).join('')||'<li class="cm-empty">没有匹配的课程</li>';
  $('#cmJoined').innerHTML=members.map(c=>row(c,true)).join('')||'<li class="cm-empty">名单为空或无匹配课程</li>';
  $('#cmAvailN').textContent=avail.length;$('#cmJoinedN').textContent=members.length;
}
$('#openCourses').onclick=guarded(async()=>{await loadCourses();openDlg($('#dlgCourses'));});
$('#cmTabs').onclick=e=>{const b=e.target.closest('[data-tab]');if(b){cmTab=b.dataset.tab;renderCourses();}};
$('#cmTabs').onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const keys=['course','quiz','elective'];cmTab=keys[(keys.indexOf(cmTab)+(e.key==='ArrowLeft'?2:1))%3];renderCourses();$(`[data-tab="${cmTab}"]`).focus();}};
$('#cmQ').oninput=renderCourses;
function changeList(lid,action){
  const tab=cmTab,kind=tab==='quiz'?'test':tab;
  confirmDlg({title:`${action==='apply'?'加入':'移出'}${listInfo[tab].label}？`,body:`课程：${lid}。${listInfo[tab].desc}`,ok:'确认'+(action==='apply'?'加入':'移出'),action:async()=>{
    const res=requireOk(await api('/api/blacklist',{kind,action,lids:[lid]}));
    if(res.unknown?.length || res.unknown_lids?.length)throw new Error('课程未能唯一匹配，请使用课程编号');
    await loadCourses();await refreshList();toast('名单已保存');
  }});
}
$('.cm-cols').onclick=e=>{const b=e.target.closest('[data-lid]');if(b)changeList(b.dataset.lid,b.dataset.action);};
$('#cmAdd').onsubmit=e=>{e.preventDefault();const lid=$('#cmAddInput').value.trim();if(lid)changeList(lid,'apply');};
$('#refreshCourses').onclick=guarded(async()=>{await runTask('refresh-courses');await loadCourses();toast('课程表已刷新');});
function manualDate(value) {
  const parts = /^(\d{4})[-/](\d{2})[-/](\d{2})$/.exec(value.trim());
  if (!parts) return '';
  const [, year, month, day] = parts;
  const date = new Date(`${year}-${month}-${day}T00:00:00`);
  if (date.getFullYear() !== Number(year) || date.getMonth() + 1 !== Number(month)
      || date.getDate() !== Number(day)) return '';
  return `${year}-${month}-${day}`;
}
$('#fDate').addEventListener('input', () => {
  $('#fDatePicker').value = manualDate($('#fDate').value);
});
$('#fDatePicker').addEventListener('change', event => {
  $('#fDate').value = event.target.value.replaceAll('-', '/');
});
$('#fDateChoose').onclick = () => {
  $('#fDatePicker').value = manualDate($('#fDate').value);
  try { $('#fDatePicker').showPicker(); }
  catch (error) { toast('请按 yyyy/mm/dd 输入截止日期'); }
};
function openForm(edit){
  const r=edit?currentItem():null;if(edit&&!isManual(r))return;
  formItem=r?{...r}:null;$('#fmTitle').textContent=r?'编辑作业':'添加作业';$('#fmSub').textContent=r?r.course+' · '+r.title:'补录平台上没有的作业，科目可自由输入';
  $('#fCourse').value=r?.course||'';$('#fTitle').value=r?.title||'';
  $('#fDate').value=(r?.deadline?.slice(0,10)||'').replaceAll('-', '/');$('#fTime').value=r?.deadline?.slice(11,16)||'23:59';$('#fNote').value=r?.note||'';
  if(!edit&&recoveredForm){for(const id of ['fCourse','fTitle','fDate','fTime','fNote'])if(id in recoveredForm)$('#'+id).value=recoveredForm[id];recoveredForm=null;}
  $('#fDatePicker').value=manualDate($('#fDate').value);
  formInitial=formSnapshot();$('#dlgForm .note').textContent='';openDlg($('#dlgForm'));$('#fCourse').focus();
}
$('#addBtn').onclick=()=>openForm(false);$('#editBtn').onclick=()=>openForm(true);
$('#hwForm').onsubmit=guarded(async e=>{
  e.preventDefault();if(formSaving)return;
  const course=$('#fCourse').value.trim(),title=$('#fTitle').value.trim(),date=manualDate($('#fDate').value),time=$('#fTime').value;
  if(!course||!title||!date||!time){$('#dlgForm .note').textContent='请填写科目、标题和有效截止日期时间';return;}
  formSaving=true;$('#hwForm [type="submit"]').disabled=true;
  try{requireOk(await api('/api/manual',{action:formItem?'edit':'add',id:formItem?.manual_id,course,title,deadline:date+' '+time,note:$('#fNote').value}));$('#dlgForm').close();await refreshList();toast('作业已保存');}
  catch(e){$('#dlgForm .note').textContent=e.message;}
  finally{formSaving=false;$('#hwForm [type="submit"]').disabled=false;}
});
for(const [button,action,label] of [['doneBtn','done','标记完成'],['delBtn','rm','删除']])$('#'+button).onclick=()=>{
  const r=currentItem();if(!isManual(r))return;
  confirmDlg({title:label+'这条作业？',body:r.course+' · '+r.title+'。完成或删除后不再进入提醒。',ok:label,danger:action==='rm',action:async()=>{requireOk(await api('/api/manual',{action,id:r.manual_id}));await refreshList();toast('已'+label);}});
};
const reviewDialog=document.createElement('dialog');reviewDialog.className='dlg';reviewDialog.id='dlgReview';reviewDialog.style.width='650px';
reviewDialog.innerHTML='<header class="dlg-head"><div><h3>确认推送内容</h3><p id="reviewMeta"></p></div><button class="x" id="reviewClose">关闭</button></header><div class="scroll"><textarea id="reviewText" rows="8" maxlength="1200" aria-label="最终推送正文"></textarea><p id="reviewStatus" role="status"></p></div><footer class="dlg-foot"><button class="btn ghost" id="reviewAuto">恢复自动</button><button class="btn line" id="reviewOmit">不推正文</button><button class="btn solid" id="reviewSave">保存正文</button></footer>';
document.body.append(reviewDialog);let reviewItem=null,reviewInitial='',reviewSaving=false;
function reviewDirty(){return reviewItem&&$('#reviewText').value!==reviewInitial;}
function closeReview(){if(reviewSaving)return;if(canClose(reviewDialog)){if(reviewItem)drafts.set(reviewItem.item_key,$('#reviewText').value);reviewDialog.close();}}
$('#reviewClose').onclick=closeReview;reviewDialog.oncancel=e=>{e.preventDefault();closeReview();};
$('#reviewBtn').onclick=guarded(async()=>{
  const r=currentItem();if(!r||isManual(r)||r.kind==='test')return;
  const c=requireOk(await api(itemUrl(r,'content')));reviewItem={...r,source_version:c.source_version};
  $('#reviewMeta').textContent=r.course+' · '+r.title;$('#reviewText').value=drafts.get(r.item_key)??c.summary?.text??c.text??'';reviewInitial=$('#reviewText').value;
  $('#reviewStatus').textContent='绑定当前原文版本保存；保存不会立即发送。';openDlg(reviewDialog);
});
for(const [id,mode] of [['reviewAuto','auto'],['reviewOmit','omit'],['reviewSave','include']])$('#'+id).onclick=guarded(async()=>{
  if(reviewSaving)return;reviewSaving=true;
  try{const res=await api(itemUrl(reviewItem,'review'),{expected_version:reviewItem.source_version,mode,text:$('#reviewText').value});
    if(!res.ok){$('#reviewStatus').textContent='未保存：'+(res.error||'请求失败')+'。当前草稿保留，请刷新正文后重新复核。';drafts.set(reviewItem.item_key,$('#reviewText').value);return;}
    drafts.delete(reviewItem.item_key);reviewDialog.close();await refreshList();toast('正文确认已保存');
  }finally{reviewSaving=false;}
});
const crop={item:null,asset:null,box:[0,0,1,1],other:[],regions:[],index:0,dirty:false,saving:false,reset:false};
function cropRegionOptions(){const select=$('#cropRegion');select.innerHTML=crop.regions.map((r,i)=>`<option value="${i}">区域 ${i+1}</option>`).join('');select.value=String(crop.index);}
function drawCrop(){
  const [x1,y1,x2,y2]=crop.box,r=$('.cr-rect'),img=$('#cropImage');
  Object.assign(r.style,{left:x1*100+'%',top:y1*100+'%',width:(x2-x1)*100+'%',height:(y2-y1)*100+'%'});r.hidden=crop.reset;
  $('#cropSize').textContent=crop.reset?'恢复使用原图':`选区 ${Math.ceil(x2*img.naturalWidth)-Math.floor(x1*img.naturalWidth)} × ${Math.ceil(y2*img.naturalHeight)-Math.floor(y1*img.naturalHeight)} px`;
  $('#cropOriginalSize').textContent=`原图 ${img.naturalWidth} × ${img.naturalHeight} px`;
}
async function openImage(id,cropping){
  const r=currentItem();if(!r)return;
  const c=requireOk(await api(itemUrl(r,'content')));if(!c.asset_ids.includes(id))throw new Error('图片不属于当前正文');
  const safe=imageMeta(c.blocks,id)?.crop_safe!==false;
  if(cropping&&!safe)throw new Error('图片尚未通过方向校验，请安装 Pillow 后刷新正文');
  $('#viewCrop').disabled=!safe;
  crop.item={...r,source_version:c.source_version,view:c.view};crop.asset=id;
  if(!cropping){$('#viewImage').src=assetUrl(id);$('#viewMeta').textContent=r.course+' · '+r.title;$('#viewNote').textContent=(c.view?.regions||[]).some(x=>x.asset_id===id)?'后续群提醒使用已保存裁剪，原图保留':'后续群提醒使用原图';openDlg($('#dlgView'));return;}
  $('#dlgView').close();crop.other=(c.view?.regions||[]).filter(x=>x.asset_id!==id);
  crop.regions=(c.view?.regions||[]).filter(x=>x.asset_id===id).map(x=>({...x,box:[...x.box]}));if(!crop.regions.length)crop.regions=[{asset_id:id,box:[0,0,1,1],origin:'manual'}];
  crop.index=0;crop.box=[...crop.regions[0].box];crop.dirty=false;crop.reset=false;cropRegionOptions();
  $('#cropStatus').textContent='拖动图片框选，拖动角柄调整。可添加多个区域；恢复原图会清除本图所有区域。';
  $('#cropImage').src=assetUrl(id);await $('#cropImage').decode();openDlg($('#dlgCrop'));drawCrop();
}
$('#cropImage').onload=drawCrop;
document.addEventListener('click',guarded(async e=>{const v=e.target.closest('[data-view]'),c=e.target.closest('[data-crop]');if(v)await openImage(v.dataset.view,false);if(c)await openImage(c.dataset.crop,true);}));
$('#viewCrop').onclick=guarded(()=>openImage(crop.asset,true));
let drag=null;
const point=e=>{const r=$('#cropImage').getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];};
$('.cr-stage').onpointerdown=e=>{if(crop.saving)return;e.preventDefault();const p=point(e);drag={start:p,box:[...crop.box],handle:e.target.dataset.handle};$('.cr-stage').setPointerCapture(e.pointerId);crop.reset=false;};
$('.cr-stage').onpointermove=e=>{
  if(!drag)return;const p=point(e);let a=drag.start,b=p;
  if(drag.handle){const [x1,y1,x2,y2]=drag.box;a=[drag.handle.includes('l')?x2:x1,drag.handle.includes('t')?y2:y1];}
  crop.box=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];crop.dirty=true;drawCrop();
  crop.regions[crop.index].box=[...crop.box];
};
$('.cr-stage').onpointerup=()=>{drag=null;};$('.cr-stage').onpointercancel=()=>{drag=null;};
$('#cropReset').onclick=()=>{crop.reset=true;crop.dirty=true;drawCrop();};
$('#cropRegion').onchange=()=>{crop.index=Number($('#cropRegion').value);crop.box=[...crop.regions[crop.index].box];crop.reset=false;drawCrop();};
$('#cropAdd').onclick=()=>{if(crop.regions.length+crop.other.length>=20){toast('最多保存 20 个区域');return;}crop.regions.push({asset_id:crop.asset,box:[0,0,1,1],origin:'manual'});crop.index=crop.regions.length-1;crop.box=[0,0,1,1];crop.reset=false;crop.dirty=true;cropRegionOptions();drawCrop();};
$('#cropRemove').onclick=()=>{crop.regions.splice(crop.index,1);crop.index=0;crop.dirty=true;if(!crop.regions.length){crop.reset=true;crop.regions=[{asset_id:crop.asset,box:[0,0,1,1],origin:'manual'}];}crop.box=[...crop.regions[0].box];cropRegionOptions();drawCrop();};
$('#cropSave').onclick=guarded(async()=>{
  if(crop.saving)return;if(!crop.reset&&(crop.box[2]<=crop.box[0]||crop.box[3]<=crop.box[1])){toast('请选择有效区域');return;}
  crop.saving=true;$('#cropSave').disabled=true;
  try{
    const regions=[...crop.other,...(crop.reset?[]:crop.regions)];
    const res=await api(itemUrl(crop.item,'view'),{expected_version:crop.item.source_version,expected_view_revision:crop.item.view?.revision||'none',regions,folded_block_ids:crop.item.view?.folded_block_ids||[],summary:crop.item.view?.summary||''});
    if(!res.ok){$('#cropStatus').textContent='未保存：'+res.error+'；选区草稿仍保留。';return;}
    crop.dirty=false;$('#dlgCrop').close();await refreshList();toast(crop.reset?'已恢复原图，后续提醒生效':'裁剪已保存，后续提醒使用裁剪图');
  }finally{crop.saving=false;$('#cropSave').disabled=false;}
});
