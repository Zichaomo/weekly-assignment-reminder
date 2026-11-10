'use strict';
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = () => { try { window.renderLocalIcons?.(); } catch(e) { console.warn('图标暂不可用'); } };
const state = {allRows:[],selected:null,courses:[],lists:{},preview:null,target:'class',logs:[],tasks:new Map(),listSeq:0,channel:null,coverage:null,schedule:null};
const currentItem = () => state.allRows.find(x=>x.item_key===state.selected);
const isManual = r => r?.source === '手动';
const submittedLabel = r => ({submitted:'本人已提交',unsubmitted:'本人未提交'}[r.submit_state] || '本人提交状态未知');
const assetUrl = id => '/api/assets/'+encodeURIComponent(id);
const itemUrl = (r,suffix) => '/api/assignments/'+encodeURIComponent(r.item_key)+'/'+suffix;
function imageMeta(blocks,id){let result=null;function visit(x){if(!x||typeof x!=='object')return;if(x.type==='image'&&x.asset_id===id)result=x;for(const value of Object.values(x)){if(Array.isArray(value))value.forEach(visit);else if(value&&typeof value==='object')visit(value);}}visit(blocks);return result;}
const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));
async function api(path,data) {
  try {
    const response = await fetch(path,{...(data ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)} : {}),signal:AbortSignal.timeout(20000)});
    const result = await response.json();
    return {...result,ok:response.ok && result.ok !== false,http_status:response.status};
  } catch(e) { return {ok:false,error:'本地服务连接失败：'+e.message,network:true}; }
}
function requireOk(result) { if (!result.ok) throw new Error(result.error || result.output || '操作失败'); return result; }
function log(text) { state.logs.push(new Date().toLocaleTimeString('zh-CN')+' '+text); state.logs=state.logs.slice(-200); if ($('#logText')) $('#logText').textContent=state.logs.join('\n'); }
let toastTimer;
function toast(text) { const t=$('#toast'); t.querySelector('span').textContent=text; if(t.showPopover && !t.matches(':popover-open'))t.showPopover(); t.classList.add('show'); clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),4000); }
function report(e) { log(e.message || String(e));toast(e.message || String(e)); }
function guarded(fn) { return async (...args) => { try { await fn(...args); } catch(e) { report(e); } }; }
function setPop(btn,pop,open) { pop.classList.toggle('open',open);btn.setAttribute('aria-expanded',String(open)); }
const pvBtn=$('#pvBtn'),pv=$('#preview'),menuBtn=$('#menuBtn'),menu=$('#menu');
function closeAllPops(){setPop(pvBtn,pv,false);setPop(menuBtn,menu,false);}
menuBtn.onclick=e=>{e.stopPropagation();setPop(pvBtn,pv,false);setPop(menuBtn,menu,!menu.classList.contains('open'));};
[pv,menu].forEach(p=>p.onclick=e=>e.stopPropagation());
document.addEventListener('click',closeAllPops);
$('#pvClose').onclick=()=>{setPop(pvBtn,pv,false);pvBtn.focus();};
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')closeAllPops();
  if(pv.classList.contains('open') && e.key==='Tab'){
    const buttons=[...pv.querySelectorAll('button:not(:disabled)')];
    if(e.shiftKey && document.activeElement===buttons[0]){e.preventDefault();buttons.at(-1).focus();}
    if(!e.shiftKey && document.activeElement===buttons.at(-1)){e.preventDefault();buttons[0].focus();}
  }
});
function elective(r){return !!r.elective;}
function tags(r){return `<span class="src ${isManual(r)?'manual':r.kind==='test'?'quiz':'platform'}">${isManual(r)?'手动补录':r.kind==='test'?'在线测试':'平台作业'}</span>${elective(r)?'<span class="src elective">选修</span>':''}`;}
function due(r){const dt=new Date((r.deadline || '').replace(' ','T'));return Number.isNaN(+dt)?null:dt;}
function selectedDays() {
  return Math.max(1, Math.min(30, Math.round(Number($('#numDays').value) || 7)));
}
function monthDay(date) {
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}
function weekday(date) {
  return ['周日','周一','周二','周三','周四','周五','周六'][date.getDay()];
}
function timeText(date) {
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}
function dayOffset(date, now) {
  return (Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
    - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 864e5;
}
function deadlineParts(row, now = new Date()) {
  const date = due(row);
  if (!date) return { date: null, group: 'undated', heading: '未设截止', remain: '未设截止', short: '待办', heat: '' };
  const milliseconds = date - now;
  const offset = dayOffset(date, now);
  const expired = milliseconds < 0;
  const minutes = Math.max(0, Math.floor(milliseconds / 6e4));
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  const relative = offset === 0 ? '今天' : offset === 1 ? '明天' : offset === 2 ? '后天' : weekday(date);
  const heat = !expired && offset === 0 ? 'hot' : !expired && offset === 1 ? 'warm' : '';
  const remain = expired ? '已逾期' : hours < 24
    ? `${hours} 小时 ${minutes % 60} 分` : `${days} 天 ${hours % 24} 小时`;
  const short = expired ? '已逾期' : hours < 24 ? `剩 ${hours} 小时` : `剩 ${days} 天 ${hours % 24} 时`;
  const group = expired ? 'expired' : offset === 0 ? 'today' : offset === 1 ? 'tomorrow' : 'later';
  const heading = group === 'expired' ? '已过期' : group === 'later' ? '本周稍后'
    : `${relative} · ${monthDay(date)} ${weekday(date)}`;
  const progress = Math.max(4, Math.min(100, 100 - milliseconds / (selectedDays() * 864e5) * 100));
  return { date, offset, expired, relative, heat, remain, short, group, heading, progress };
}
function windowRows() {
  const now = Date.now();
  const end = now + selectedDays() * 864e5;
  return state.allRows.filter(row => {
    const date = due(row);
    return !date || (date <= end && ($('#chkAll').checked || date >= now));
  }).sort((left, right) => (due(left)?.getTime() ?? Infinity) - (due(right)?.getTime() ?? Infinity));
}
function visibleRows() {
  const query = $('#q').value.trim().toLowerCase();
  return windowRows().filter(row => !query || (row.course + row.title).toLowerCase().includes(query));
}
function deadlineMarkup(row) {
  const parts = deadlineParts(row);
  const remaining = parts.remain.split(' ').map(token => /^\d+$/.test(token)
    ? `<span class="deadline-number">${token}</span>` : esc(token)).join(' ');
  return `
    <div class="deadline-info">
      <div><div class="k">截止时间</div>${parts.date
        ? `<time class="v deadline-date" datetime="${parts.date.toISOString()}">${monthDay(parts.date)} ${weekday(parts.date)} ${timeText(parts.date)}</time>`
        : '<div class="v">未设截止</div>'}</div>
      ${parts.date ? `<div class="count"><div class="k">${parts.expired ? '状态' : '还剩'}</div><div class="v ${parts.heat}">${remaining}</div></div>` : ''}
    </div>
    ${parts.date ? `<div class="timeline"><div class="bar ${parts.heat}" role="progressbar" aria-label="临近截止的时间进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Number(parts.progress.toFixed(3))}" aria-valuetext="${parts.expired ? '已逾期' : '剩余 ' + parts.remain}"><i style="width:${parts.progress}%"></i></div></div>` : ''}`;
}
function render(){
  state.renderedDay = new Date().toDateString();
  for (const id of ['chkAll', 'chkTests']) {
    const input = $('#'+id);
    input.closest('.chip').setAttribute('aria-pressed', String(input.checked));
  }
  const rows=visibleRows();
  if(!rows.some(r=>r.item_key===state.selected))state.selected=rows[0]?.item_key || null;
  const groups = new Map();
  for (const row of rows) {
    const parts = deadlineParts(row);
    if (!groups.has(parts.group)) groups.set(parts.group, { heading: parts.heading, rows: [] });
    groups.get(parts.group).rows.push({ row, parts });
  }
  $('#list').innerHTML = [...groups].map(([key, group]) => {
    let heading = esc(group.heading);
    if (key === 'today') heading = '<span class="tag-hot">今天</span>' + esc(group.heading.slice(2));
    if (key === 'later') {
      const first = monthDay(group.rows[0].parts.date);
      const last = monthDay(group.rows.at(-1).parts.date);
      heading += ' · ' + first + (first === last ? '' : ' — ' + last);
    }
    return `
    <div class="group" role="group">
      <div class="group-h">${heading}</div>
      ${group.rows.map(({ row, parts }) => `
        <button class="item" role="option" aria-selected="${row.item_key === state.selected}" data-key="${esc(row.item_key)}" data-group="${parts.group}">
          <span class="when"><span class="t">${parts.date ? timeText(parts.date) : '—'}</span>
            <span class="d">${parts.offset === 0 || parts.offset === 1 ? parts.relative : esc(parts.date ? weekday(parts.date) + ' ' + (parts.date.getMonth()+1) + '/' + parts.date.getDate() : '')}</span></span>
          <span class="meta"><span class="course"><span class="c">${esc(row.course)}</span>${tags(row)}</span><span class="title">${esc(row.title)}</span></span>
          <span class="remain ${parts.heat}">${parts.short}</span>
        </button>`).join('')}
    </div>`;
  }).join('') || '<div class="empty">没有匹配的作业</div>';
  const all = windowRows();
  const count = predicate => all.filter(predicate).length;
  $('#stats').innerHTML = `
    <span>共<b>${all.length}</b></span>
    <span class="hot">今天截止<b>${count(row => deadlineParts(row).offset === 0)}</b></span>
    <span>明天截止<b>${count(row => deadlineParts(row).offset === 1)}</b></span>
    <span>在线测试<b>${count(row => row.kind === 'test')}</b></span>
    <span>选修<b>${count(elective)}</b></span>
    ${$('#q').value.trim() ? `<span class="filter-stat">匹配<b>${rows.length}</b></span>` : ''}`;
  $('#listFoot').textContent = `来源：北化在线 ${count(row => !isManual(row))} · 手动补录 ${count(isManual)}`;
  const now = new Date();
  $('#today').textContent = `${now.getFullYear()}年${monthDay(now)} · ${weekday(now)}`;
  renderDetail();icons();
}
function renderDetail(){
  const r=currentItem();
  document.querySelectorAll('[data-for="platform"]').forEach(b=>b.hidden=!r||isManual(r));
  document.querySelectorAll('[data-for="manual"]').forEach(b=>b.hidden=!isManual(r));
  $('#reviewBtn').hidden=!r||isManual(r)||r.kind==='test';
  $('#retryCourse').disabled=!r?.lid;
  $('#footNote').textContent=r?(isManual(r)?'手动补录 · 仅保存在本机':r.content_stale?'正文已过期，请刷新':r.content_error || '本人已提交也会提醒班级同学'):'';
  if(!r){$('#detail').innerHTML='<div class="empty-detail">选择作业查看详情</div>';return;}
  const summary = r.content_summary?.text || r.note || (r.content_summary?.status === 'images'
    ? '作业要求见下方图片。' : isManual(r) ? '未填写备注' : r.content_summary?.reason || '正文尚未就绪，可刷新正文查看。');
  $('#detail').innerHTML = `
    <div class="detail-inner fade">
      <div class="eyebrow">${esc(r.course)}${tags(r)}</div><h2>${esc(r.title)}</h2>
      <div class="deadline">${deadlineMarkup(r)}</div>
      <div class="flags">
        ${isManual(r) ? '' : `<span class="${r.submit_state === 'submitted' ? 'done' : ''}"><i data-lucide="${r.submit_state === 'submitted' ? 'circle-check' : 'circle-dashed'}"></i>${submittedLabel(r)}</span>`}
        ${elective(r) ? '<span class="off"><i data-lucide="circle-minus"></i>不进入班级提醒（选修）</span>' : '<span class="on"><i data-lucide="users"></i>进入班级提醒</span>'}
        <span class="on"><i data-lucide="user"></i>进入个人提醒</span>
      </div>
      <div class="sec"><div class="sec-h">任务摘要</div><p class="summary">${esc(summary)}</p></div>
      <div class="sec"><div class="sec-h">${isManual(r) ? '补录原文' : '北化在线原文'}${isManual(r) ? '' : '<button class="link" data-refresh-content><i data-lucide="rotate-cw"></i>刷新正文</button>'}</div>
        <div class="origin" id="origin"></div><span class="source-time" id="contentTime"></span>
      </div>
      <div class="sec" id="itemImages" hidden></div>
    </div>`;
  const origin=$('#origin');
  if(isManual(r))origin.textContent=r.note || '无备注';
  else if(r.content_ready&&!r.content_stale){
    (r.content_blocks || []).forEach(b=>renderBlock(b,origin));
    if(!origin.textContent.trim())origin.textContent=r.content_text || '';
    origin.hidden=!origin.textContent.trim()&&!origin.querySelector('table,button');
  }
  else origin.textContent=r.content_error || (r.content_stale?'本地正文已过期，请刷新。':'尚未缓存正文，打开详情不会自动访问平台。');
  if (r.content_checked_at) {
    const checked = new Date(r.content_checked_at);
    if (!Number.isNaN(+checked)) $('#contentTime').textContent = `抓取于 ${monthDay(checked)} ${timeText(checked)} · 北化在线`;
  }
  if(!isManual(r)&&r.content_ready&&!r.content_stale){
    const imageIds = r.asset_ids || [];
    $('#itemImages').hidden=!imageIds.length;
    $('#itemImages').innerHTML=imageIds.length ? `<div class="sec-h">作业图片<span class="num">${imageIds.length} 张</span></div>` : '';
    for(const id of (r.asset_ids||[])){
      const crops=(r.view?.regions||[]).filter(x=>x.asset_id===id);
      const figure=document.createElement('figure');figure.className='shot';
      figure.innerHTML=`<button class="shot-img" data-view="${esc(id)}" aria-label="查看原图"><img src="${assetUrl(id)}" alt="作业原图" /></button><figcaption><span>${crops.length?'已保存裁剪 · 后续提醒使用裁剪':'后续提醒使用原图'}</span><span class="shot-acts"><button class="btn ghost sm" data-view="${esc(id)}">查看原图</button><button class="btn line sm" data-crop="${esc(id)}" ${imageMeta(r.content_blocks,id)?.crop_safe===false?'disabled title="需安装 Pillow 并刷新正文"':''}>裁剪图片</button></span></figcaption>`;
      $('#itemImages').append(figure);
    }
  }
  $('#detail [data-refresh-content]')?.addEventListener('click', () => $('#refreshContent').click());
}
// Only explicit safe nodes and platform links are rendered. Cached HTML is never inserted.
function renderSegments(segments,parent){for(const s of segments||[]){if(s.type==='image')continue;if(s.type==='link'&&s.url){const a=document.createElement('button');a.className='link';a.textContent=s.text||s.url;a.onclick=()=>openSafeLink(s.url);parent.append(a);}else parent.append(document.createTextNode(s.text||''));}}
function renderBlock(b,parent){
  if(b.type==='image')return;
  const el=document.createElement(b.type==='table'?'table':b.type==='list'?(b.ordered?'ol':'ul'):b.type==='pre'?'pre':'p');
  if(b.type==='table'){
    for(const row of b.cells||b.rows||[]){const tr=document.createElement('tr');for(const c of row){const td=document.createElement(c?.header?'th':'td');if(c?.blocks?.length)c.blocks.forEach(x=>renderBlock(x,td));else td.textContent=typeof c==='object'?c.text||'':String(c);tr.append(td);}el.append(tr);}
  }else if(b.type==='list')for(const it of b.items||[]){const li=document.createElement('li');if(it.blocks)it.blocks.forEach(x=>renderBlock(x,li));else if(it.segments)renderSegments(it.segments,li);else li.textContent=it.text||String(it);el.append(li);}
  else if(b.type==='attachment'){const a=document.createElement('button');a.className='link';a.textContent=b.text||'附件';a.onclick=()=>openSafeLink(b.url);el.append(a);}
  else if(b.segments)renderSegments(b.segments,el);else el.textContent=b.text||'';
  if (el.textContent.trim() || el.querySelector('table,button')) parent.append(el);
}
const openSafeLink=guarded(async url=>{requireOk(await api('/api/open',{url}));});
function renderCacheStatus() {
  const coverage = state.coverage || {};
  const updated = new Date(coverage.updated_at || '');
  const available = !Number.isNaN(+updated);
  const stale = !coverage.publishable;
  $('#cacheStatus').innerHTML = `<i class="dot ${stale ? 'stale' : ''}"></i>${available ? '数据更新于 <span class="num">' + timeText(updated) + '</span>' : '尚无检索记录'}`;
  $('#cacheStatus').title = available ? `数据更新于 ${updated.toLocaleString('zh-CN')}\n${(coverage.publish_block_reasons || []).join('，')}` : '请检索北化在线获取数据';
}
function renderScheduleStatus(schedule) {
  state.schedule = schedule;
  const weekly = (schedule.tasks || []).find(task => task.name === 'BUCT-Homework-Weekly');
  let text = '定时提醒未开启';
  if (!schedule.ok || !schedule.available) text = '定时状态暂时无法读取';
  else if (weekly?.registered && weekly.enabled) text = weekly.schedule_time
    ? `每周日 ${weekly.schedule_time} 自动提醒` : '定时提醒已开启';
  $('#scheduleStatus').textContent = text;
  $('#scheduleStatus').title = weekly?.enabled && weekly.next_run ? `下次检查 ${weekly.next_run}` : text;
  $('#previewNext').textContent = weekly?.enabled && weekly.next_run ? `下次定时检查\n${weekly.next_run}` : text;
}
document.addEventListener('homework:schedule', event => renderScheduleStatus(event.detail));
async function refreshList(){
  const seq=++state.listSeq;
  const res=requireOk(await api(`/api/list?days=${selectedDays()}&include_expired=${$('#chkAll').checked?1:0}`));
  if(seq!==state.listSeq)return;
  state.allRows=res.rows||[];state.preview=null;render();
  const cov=res.cache || res.coverage || {};
  state.coverage = { ...cov, updated_at: res.data_fetched_at || cov.updated_at };
  renderCacheStatus();
  $('#coverage').textContent=(cov.publish_block_reasons||[]).join('；') || (cov.publishable?'缓存检查通过，发布时将再次校验':'本地缓存；预览和发布将检查覆盖与新鲜度');
  const cache=await api('/api/cache');
  if(seq !== state.listSeq) return;
  if(cache.ok && !state.coverage.updated_at) {
    state.coverage = cache.coverage;
    renderCacheStatus();
  }
}
const taskLabels = { preview:'本地预览', publish:'发布提醒', fetch:'检索北化在线', content:'获取正文', 'refresh-courses':'刷新课程表', login:'学校登录', 'check-login':'检查登录' };
function taskProgressText(task) {
  const progress = task.progress || {};
  const done = progress.done ?? progress.course_done;
  const total = progress.total ?? progress.course_total;
  return Number.isFinite(done) && Number.isFinite(total) ? ` · 已完成 ${done}/${total} 门课程` : '';
}
function taskFailure(task, result, operation) {
  if (typeof result?.error === 'string') return result.error;
  if (typeof task.error === 'string') return task.error;
  if (operation === 'publish' && task.status === 'partial') return result?.personal?.error || '部分提醒发布失败，请查看运行记录后重试';
  if (!result) return '任务结果暂时无法恢复，请刷新列表或重新预览';
  return `${taskLabels[operation] || operation}未完成，请重试`;
}
function showTaskStatus(){const list=[...state.tasks.values()];$('#taskStatus').hidden=!list.length;$('#taskStatus').textContent=list.map(t=>t.text).join(' · ');}
async function runTask(op,params={},options={}){
  const res=requireOk(await api('/api/tasks',{op,...params}));
  return pollTask(res.task_id,op,options);
}
async function pollTask(id,op,options={}){
  let terminal=false;
  const label = taskLabels[op] || op;
  state.tasks.set(id,{op,text:label+' · 排队中'});showTaskStatus();
  sessionStorage.setItem('homeworkUiTasks',JSON.stringify([...state.tasks].map(([id,t])=>({id,op:t.op}))));
  const deadline=Date.now()+(op === 'login' ? 660000 : 180000);
  try {
    while(Date.now()<deadline){
      const task=requireOk(await api('/api/tasks/'+encodeURIComponent(id)));
      state.tasks.set(id,{op,text:`${label} · ${task.status === 'running' ? '运行中' : '排队中'}${taskProgressText(task)}`});showTaskStatus();
      if(!['queued','running','already-active'].includes(task.status)){
        terminal=true;
        const result=task.result;
        // Coverage blocks publication, but a successful local preview can still
        // show its stale snapshot. Other partial operations retain failure checks.
        if(op === 'preview' && result?.ok === true && typeof result.message === 'string'
            && ['done','partial'].includes(task.status)) {
          log(result.cache_blocked ? '本地预览已生成，发布前需更新缓存' : '本地预览已生成');
          return result;
        }
        if(options.allowPartial&&op==='content'&&task.status==='partial'&&result?.failed){log('正文部分失败：'+(result.error||result.failed)+'；不会重复尝试本批条目');return result;}
        if(!result || result.ok===false || ['failed','error','interrupted','partial'].includes(task.status))throw new Error(taskFailure(task, result, op));
        log(op+'完成'+(result.sent?'（有发送）':result.deduped?'（去重跳过）':''));return result;
      }
      await delay(500);
    }
    throw new Error('等待任务超时；后台任务可能仍在运行，可重新打开页面继续查看。');
  } finally {if(terminal)state.tasks.delete(id);else state.tasks.set(id,{op,text:op+'：等待中断，可重新打开页面继续查看'});showTaskStatus();sessionStorage.setItem('homeworkUiTasks',JSON.stringify([...state.tasks].map(([id,t])=>({id,op:t.op}))));}
}
async function preview(){state.preview=await runTask('preview',{days:selectedDays()});renderPreview();}
function renderPreviewMessage(container, message, images) {
  container.replaceChildren();
  const groups = new Map();
  for (const image of images) {
    if (!groups.has(image.item_key)) groups.set(image.item_key, []);
    groups.get(image.item_key).push(image);
  }
  // Match the original console and WeCom layout: replace each image marker in
  // place, so its pictures stay before the next homework and the final count.
  const ordered = [...groups.values()].sort((left, right) =>
    message.indexOf('｜' + left[0].title) - message.indexOf('｜' + right[0].title));
  const parts = message.split(/^   作业(?:原图|图片)随提醒发送（\d+ 张）。$/m);
  const appendImage = entry => {
    const image = document.createElement('img');
    image.src = assetUrl(entry.delivery_asset_id || entry.asset_id);
    image.alt = `${entry.course}｜${entry.title}`;
    image.style.display = 'block';
    image.style.margin = '8px 0';
    container.append(image);
  };
  if (parts.length === ordered.length + 1) {
    parts.forEach((part, index) => {
      container.append(document.createTextNode(part));
      for (const entry of ordered[index] || []) appendImage(entry);
    });
  } else {
    container.append(document.createTextNode(message));
    images.forEach(appendImage);
  }
}
function renderPreview(){
  const p=state.preview;if(!p)return;
  $('#seg').dataset.v=state.target;
  $('#seg').querySelectorAll('button').forEach(b=>{b.setAttribute('aria-selected',String(b.dataset.v===state.target));b.querySelector('b').textContent=b.dataset.v==='class'?p.count:p.count_personal;});
  $('#pvMeta').textContent=`${state.target==='class'?'班级提醒 · 排除选修':'个人完整提醒 · 含选修'} · 班级渠道 ${state.channel||'未知'}`;
  renderPreviewMessage($('#bubble'),
    (state.target==='class'?p.message:p.message_personal) || '',
    (state.target==='class'?p.images:p.images_personal) || []);
  const reasons = p.coverage?.publish_block_reasons || [];
  $('#previewNotice').textContent = p.cache_blocked
    ? `缓存需要更新，本次可查看预览。${reasons.join('，')}。发布前请重新检索。`
    : '本地预览已生成，发布时将再次校验数据。';
  $('#copyBtn').disabled=false;
  $('#publish').disabled=!!p.cache_blocked;
}
pvBtn.onclick=guarded(async e=>{
  e.stopPropagation();
  setPop(menuBtn,menu,false);
  if(pv.classList.contains('open')) { setPop(pvBtn,pv,false); return; }
  state.preview=null;
  setPop(pvBtn,pv,true);
  $('#bubble').textContent='正在生成本地预览…';
  $('#previewNotice').textContent='';
  $('#publish').disabled=$('#copyBtn').disabled=true;
  try {
    await preview();
    $('#pvClose').focus();
  } catch(error) {
    $('#bubble').textContent=`预览生成失败\n${error.message}`;
    throw error;
  }
});
$('#seg').onclick=e=>{const b=e.target.closest('[data-v]');if(b){state.target=b.dataset.v;renderPreview();}};
$('#copyBtn').onclick=guarded(async()=>{if(!state.preview)return;await navigator.clipboard.writeText(state.target==='class'?state.preview.message:state.preview.message_personal);toast('已复制');});
$('#publish').onclick=guarded(async()=>{
  if(!state.preview)return;
  confirmDlg({title:'提前发布提醒？',body:'将按现有配置向班级及已启用的个人通道发送。请先核对两份本地预览。',ok:'确认发布',icon:'send',action:async()=>{const result=await runTask('publish',{days:Number($('#numDays').value)||7});toast(result.deduped?'相同内容已发送，本次去重跳过':result.sent?'发布完成，请查看运行记录':'任务完成，未报告实际发送');state.preview=result;renderPreview();}});
});
$('#list').onclick=e=>{const b=e.target.closest('[data-key]');if(b){state.selected=b.dataset.key;render();$('#detail').scrollTop=0;}};
$('#q').oninput=render;
$('#numDays').oninput=()=>{ $('#numDays').style.width=Math.max(1.4,$('#numDays').value.length*.7)+'em'; };
$('#numDays').onchange=guarded(async()=>{ $('#numDays').value=selectedDays();await refreshList(); });
for(const id of ['chkAll','chkTests']) {
  $('#'+id).onchange=guarded(async event=>{
    event.target.closest('.chip').setAttribute('aria-pressed',String(event.target.checked));
    if(id==='chkAll')await refreshList();
  });
}
$('#openPlatform').onclick=()=>{const r=currentItem();if(r)openSafeLink(r.submit_state==='submitted'?(r.result_url||r.submit_url||r.content_url):(r.submit_url||r.content_url||r.result_url));};
$('#refreshContent').onclick=guarded(async()=>{const r=currentItem();if(r){try{await runTask('content',{item_keys:[r.item_key],force:true});}finally{await refreshList();}}});
$('#retryCourse').onclick=guarded(async()=>{const r=currentItem();if(!r?.lid)return;try{await runTask('fetch',{lids:[r.lid],days:Number($('#numDays').value)||7,tests:$('#chkTests').checked});}finally{await refreshList();}});
$('#fetchBtn').onclick=guarded(async()=>{
  $('#fetchBtn').disabled=true;
  try {let result;
    try{result=await runTask('fetch',{days:Number($('#numDays').value)||7,all:$('#chkAll').checked,tests:$('#chkTests').checked});}finally{await refreshList();}
    if(result.warnings?.failed_courses?.length){log('部分课程检索失败；本次不自动补正文，请先重试课程。');toast('检索未完整，请查看覆盖提示');return;}
    const now=Date.now(),end=now+(Number($('#numDays').value)||7)*864e5;
    const keys=state.allRows.filter(r=>!isManual(r)&&r.kind!=='test'&&due(r)>=now&&due(r)<=end&&(!r.content_ready||r.content_stale||r.content_error)).map(r=>r.item_key);
    const deadline=Date.now()+180000;
    for(let i=0;i<keys.length&&i<2400;i+=12){if(Date.now()>deadline){log('本轮正文补齐达到时间上限，可手动刷新剩余条目');break;}await runTask('content',{item_keys:keys.slice(i,i+12),limit:12},{allowPartial:true});await refreshList();}
  } finally {$('#fetchBtn').disabled=false;}
});
$('#today').textContent=new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'short'});
function refreshClock() {
  const rows = visibleRows();
  const buttons = [...document.querySelectorAll('#list .item')];
  const regroup = state.renderedDay !== new Date().toDateString()
    || rows.length !== buttons.length
    || rows.some((row, index) => row.item_key !== buttons[index].dataset.key
      || deadlineParts(row).group !== buttons[index].dataset.group);
  if (regroup) {
    render();
    return;
  }
  rows.forEach((row, index) => {
    const parts = deadlineParts(row);
    const remain = buttons[index].querySelector('.remain');
    remain.textContent = parts.short;
    remain.className = 'remain ' + parts.heat;
  });
  if (currentItem() && $('.deadline')) $('.deadline').innerHTML = deadlineMarkup(currentItem());
}
window.addEventListener('focus', refreshClock);
setInterval(refreshClock, 60000);
async function heartbeat(){await api('/api/heartbeat',{});}heartbeat();setInterval(heartbeat,5000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){heartbeat();refreshClock();}});
$('#exportViews').onclick=guarded(async()=>{const data=requireOk(await api('/api/views/export'));const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.download='homework-views.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);});
$('#shutdown').onclick=()=>confirmDlg({title:'停止控制台服务？',body:'本地窗口将无法继续读取数据。',ok:'停止服务',icon:'x',action:async()=>{requireOk(await api('/api/shutdown',{}));toast('服务已请求停止');}});
icons();
