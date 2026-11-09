/* 第二轮界面：仅展示层交互，不修改数据 */
const DEMO_MSG = '界面演示，后续接入';
const byCode = c => COURSES.find(x => x.code === c);

function openDlg(d){ closeAllPops(); d.showModal(); icons(); }
function closeAllPops(){ setPop(pvBtn, pv, false); setPop(menuBtn, menu, false); }

document.querySelectorAll('.dlg').forEach(d => {
  d.addEventListener('click', e => {
    if (e.target === d) d.close();
    if (e.target.closest('[data-close]')) d.close();
    if (e.target.closest('[data-demo]')) toast(DEMO_MSG);
  });
});

/* ---------- 通用确认 ---------- */
const cf = $('#dlgConfirm');
function confirmDlg({ icon, title, body, ok, danger }){
  $('#cfIcon').className = 'cf-icon' + (danger ? ' danger' : '');
  $('#cfIcon').innerHTML = `<i data-lucide="${icon}"></i>`;
  $('#cfTitle').textContent = title;
  $('#cfBody').innerHTML = body;
  $('#cfOk').textContent = ok;
  $('#cfOk').className = 'btn solid' + (danger ? ' danger' : '');
  openDlg(cf);
  $('#cfOk').focus();
}
$('#cfOk').addEventListener('click', () => { cf.close(); toast(DEMO_MSG); });

/* ---------- 课程管理 ---------- */
const TAB_KEYS = ['course','quiz','elective'];
let cmTab = 'course', cmQuery = '';

function courseRow(c, joined){
  const others = TAB_KEYS.filter(k => k !== cmTab && LISTS[k].members.includes(c.code));
  const tag = !joined && others.length ? `<span class="src other">已在${LISTS[others[0]].label}</span>` : '';
  return `<li class="crow">
    <span class="cn"><span class="name">${c.name}${tag}</span>
      <span class="sub"><span class="num">${c.code}</span> · ${c.teacher} · ${c.credit} 学分</span></span>
    <button class="btn sm ${joined ? 'ghost' : 'line'}" data-act="${joined ? 'remove' : 'add'}" data-code="${c.code}">
      <i data-lucide="${joined ? 'minus' : 'plus'}"></i>${joined ? '移出' : '加入'}</button>
  </li>`;
}

function renderCourses(){
  const L = LISTS[cmTab], i = TAB_KEYS.indexOf(cmTab);
  const tabs = $('#cmTabs');
  tabs.dataset.i = i;
  tabs.querySelectorAll('button').forEach(b => {
    b.setAttribute('aria-selected', b.dataset.tab === cmTab);
    b.tabIndex = b.dataset.tab === cmTab ? 0 : -1;
    b.querySelector('b').textContent = LISTS[b.dataset.tab].members.length;
  });
  $('#cmDesc').innerHTML = `<i data-lucide="${L.icon}"></i><span><b>${L.label}</b>：${L.desc}</span>`;
  $('#cmAdd').hidden = cmTab !== 'elective';

  const q = cmQuery.trim().toLowerCase();
  const match = c => !q || (c.name + c.code).toLowerCase().includes(q);
  const joined = L.members.map(byCode).filter(match);
  const avail = COURSES.filter(c => !L.members.includes(c.code) && match(c));
  $('#cmAvailN').textContent = avail.length;
  $('#cmJoinedN').textContent = joined.length;
  $('#cmAvail').innerHTML = avail.map(c => courseRow(c, false)).join('') || '<li class="cm-empty">没有匹配的课程</li>';
  $('#cmJoined').innerHTML = joined.map(c => courseRow(c, true)).join('') ||
    `<li class="cm-empty">${q ? '没有匹配的课程' : '名单为空'}</li>`;
  icons();
}

$('#openCourses').addEventListener('click', () => {
  cmTab = 'course'; cmQuery = ''; $('#cmQ').value = ''; $('#cmAddInput').value = '';
  renderCourses(); openDlg($('#dlgCourses'));
});
$('#cmTabs').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  cmTab = b.dataset.tab; renderCourses();
});
$('#cmTabs').addEventListener('keydown', e => {
  if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
  const i = (TAB_KEYS.indexOf(cmTab) + (e.key === 'ArrowRight' ? 1 : 2)) % 3;
  cmTab = TAB_KEYS[i]; renderCourses();
  $(`#cmTabs [data-tab="${cmTab}"]`).focus();
});
$('#cmQ').addEventListener('input', e => { cmQuery = e.target.value; renderCourses(); });

document.querySelector('.cm-cols').addEventListener('click', e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const c = byCode(b.dataset.code), L = LISTS[cmTab], add = b.dataset.act === 'add';
  confirmDlg(add
    ? { icon:L.icon, title:`${L.addVerb}？`, ok:'确认加入',
        body:`<b>${c.name}</b>（<span class="num">${c.code}</span>）<br />${L.addEffect}` }
    : { icon:'list-minus', title:`从${L.label}移出？`, ok:'确认移出', danger:true,
        body:`<b>${c.name}</b>（<span class="num">${c.code}</span>）<br />${L.removeEffect}` });
});

$('#cmAdd').addEventListener('submit', e => {
  e.preventDefault();
  const input = $('#cmAddInput'), v = input.value.trim();
  if (!v) { input.focus(); return; }
  const c = COURSES.find(x => x.code.toLowerCase() === v.toLowerCase() || x.name === v);
  confirmDlg({ icon:'user-round', title:'加入选修课名单？', ok:'确认加入',
    body: c ? `<b>${c.name}</b>（<span class="num">${c.code}</span>）<br />${LISTS.elective.addEffect}`
            : `课程表中未找到「<b>${v.replace(/[<>&]/g,'')}</b>」，确认后将按输入内容记录，刷新课程表时再匹配。` });
});

/* ---------- 手动作业表单 ---------- */
const fm = $('#dlgForm');
$('#fCourse').innerHTML = '<option value="">选择科目</option>' +
  COURSES.map(c => `<option value="${c.code}">${c.name}</option>`).join('');

function openForm(mode){
  const edit = mode === 'edit', v = edit ? MANUAL_SAMPLE : { course:'', title:'', date:'', time:'23:59', note:'' };
  $('#fmTitle').textContent = edit ? '编辑作业' : '添加作业';
  $('#fmSub').textContent = edit ? '大学英语 · 单元练习 · 手动补录' : '补录群消息、课堂口头布置等平台上没有的作业';
  $('#fCourse').value = v.course; $('#fTitle').value = v.title;
  $('#fDate').value = v.date; $('#fTime').value = v.time; $('#fNote').value = v.note;
  openDlg(fm);
  (edit ? $('#fTitle') : $('#fCourse')).focus();
}
$('#addBtn').addEventListener('click', () => openForm('add'));
$('#editBtn').addEventListener('click', () => openForm('edit'));
$('#hwForm').addEventListener('submit', e => { e.preventDefault(); toast(DEMO_MSG); });

$('#doneBtn').addEventListener('click', () => confirmDlg({
  icon:'circle-check', title:'标记为已完成？', ok:'标记完成',
  body:'<b>大学英语 · 单元练习</b><br />完成后不再出现在近期作业和群提醒中。' }));
$('#delBtn').addEventListener('click', () => confirmDlg({
  icon:'trash-2', title:'删除这条手动作业？', ok:'删除', danger:true,
  body:'<b>大学英语 · 单元练习</b><br />删除后无法恢复，也不会再进入任何提醒。' }));

/* ---------- 图片查看 / 裁剪 ---------- */
document.addEventListener('click', e => {
  if (e.target.closest('[data-view]')) openDlg($('#dlgView'));
  if (e.target.closest('[data-crop]')) { $('#dlgView').close(); openDlg($('#dlgCrop')); }
});
