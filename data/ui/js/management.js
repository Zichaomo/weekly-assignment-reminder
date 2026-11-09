/* Keep the established schedule/version and rule editors and their guards. */
const system=document.createElement('dialog');system.className='dlg';system.id='dlgSystem';system.style.width='800px';
system.innerHTML=`<header class="dlg-head"><h3>定时发布</h3><button class="x" id="systemClose">关闭</button></header><div class="scroll">
<strong id="scheduleSummary">正在读取定时状态…</strong><div class="manage-row"><button class="btn line" id="btnScheduleSet">设置定时发布</button><button class="btn ghost" id="btnScheduleCancel" disabled>取消定时发布</button><button class="btn ghost" id="btnSystemRefresh">刷新状态</button></div><div id="scheduleActionStatus" role="status"></div><details id="scheduleDetails"><summary>展开定时状态</summary><div class="system-box" id="scheduleText"></div></details>
</div>`;
document.body.append(system);
$('#openSystem').onclick=()=>{openDlg(system);refreshSystemStatus(true);};$('#systemClose').onclick=()=>system.close();
const backend = document.createElement('dialog');
backend.className = 'dlg';
backend.id = 'dlgBackend';
backend.style.width = '800px';
backend.innerHTML = `<header class="dlg-head"><h3>后台版本</h3><button class="x" id="backendClose">关闭</button></header>
  <div class="scroll"><strong id="runtimeText">正在检查后台版本…</strong>
    <div class="manage-row"><button class="btn solid" id="btnUpdateBackend" disabled>更新后台</button>
    <button class="btn ghost" id="btnBackendRefresh">检查版本</button></div><p id="runtimeHint"></p></div>`;
document.body.append(backend);
$('#openBackend').onclick = () => { openDlg(backend);refreshSystemStatus(); };
$('#backendClose').onclick = () => backend.close();
const schedule = document.createElement('dialog');
schedule.id = 'maskSchedule';
schedule.className = 'dlg schedule-editor';
schedule.setAttribute('aria-labelledby', 'scheduleTitle');
schedule.innerHTML = `
  <div class="modal">
    <header><h3 id="scheduleTitle">设置每周日定时发布</h3><button class="btn ghost" id="scheduleClose">关闭</button></header>
    <div class="body editgrid">
      <label class="field">发布时间<input id="scheduleTime" type="time" value="09:00"></label>
      <label class="field">提醒天数<input id="scheduleDays" type="number" min="1" max="30" value="7"></label>
      <p>保存仅安排未来触发，不立即发送。首次发布后的补推检查包含在每周任务中。</p>
      <p id="scheduleEditStatus" role="status"></p>
    </div>
    <footer><button class="btn solid" id="scheduleSave">保存定时设置</button></footer>
  </div>`;
schedule.addEventListener('cancel', event => {
  event.preventDefault();
  if (!scheduleChanging) setScheduleEditor(false);
});
document.body.append(schedule);
(async()=>{
  try{const config=await api('/api/config');state.channel=config.ok?config.channel:null;await loadCourses();await refreshList();
    const pending=JSON.parse(sessionStorage.getItem('homeworkUiTasks')||'[]');
    for(const t of pending)pollTask(t.id,t.op).then(refreshList).catch(report);
  }catch(e){report(e);}
})();
