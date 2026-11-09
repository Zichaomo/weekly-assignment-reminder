/* Local backend reload and read-only scheduled task status. */
let backendInfo = null, backendUpdating = false, systemRefreshing = false;
let scheduleInfo = null, scheduleChanging = false;
const updateDraftFields = ['addC','addT','addD','addN','fCourse','fTitle','fDate','fTime','fNote','numDays','chkAll','chkTests'];
function nextScheduleAction(task) {
  if (task.name !== 'BUCT-Homework-Weekly') return '补推检查';
  if (!task.schedule_time || !task.next_run) return '定时检查';
  return task.next_run.slice(11,16) === task.schedule_time ? '首次发布检查' : '补推检查';
}
try {
  const draft = JSON.parse(sessionStorage.getItem('homeworkUpdateDraft') || 'null');
  if (draft) for (const id of updateDraftFields) {
    if (!(id in draft)) continue;
    const el = $('#' + id);
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = draft[id]; else el.value = draft[id];
  }
  sessionStorage.removeItem('homeworkUpdateDraft');
  if (draft) refreshList({silent:true});
} catch (e) {}

async function refreshSystemStatus(force = false) {
  if (systemRefreshing || backendUpdating || scheduleChanging) return;
  systemRefreshing = true;
  const runtime = await api('/api/runtime');
  if (runtime.ok) {
    backendInfo = runtime;
    $('#runtimeText').textContent = runtime.needs_update ? '有本地新版本可加载' : '后台已是本地最新版本';
    $('#runtimeHint').textContent = `运行版本 ${runtime.running_version} · 本地版本 ${runtime.disk_version}`
      + (runtime.busy ? ' · 有任务运行或排队，请完成后更新。' : ' · 更新不会检索平台或发送消息。');
    $('#btnUpdateBackend').disabled = !runtime.needs_update || runtime.busy || runtime.updating;
  } else {
    $('#runtimeText').textContent = runtime.http_status === 404 ? '后台版本较旧，请关闭服务后重新打开控制台' : '暂时无法读取后台状态';
    $('#btnUpdateBackend').disabled = true;
  }
  const schedule = await api('/api/schedule' + (force ? '?refresh=1' : ''));
  if (schedule.ok) {
    scheduleInfo = schedule;
    const weekly = (schedule.tasks || []).find(t => t.name === 'BUCT-Homework-Weekly');
    const enabled = (schedule.tasks || []).some(t => t.registered && t.enabled);
    $('#scheduleSummary').textContent = !schedule.available ? '定时状态暂时无法读取'
      : weekly?.enabled ? `定时发布已开启 · 下次${nextScheduleAction(weekly)} ${weekly.next_run || '待系统安排'}`
      : enabled ? '每周发布未开启，补推仍开启' : '定时发布未开启';
    $('#btnScheduleSet').disabled = !schedule.available || !!runtime.busy;
    $('#btnScheduleCancel').disabled = !schedule.available || !enabled || !!runtime.busy;
    const lines = [];
    if (schedule.available && weekly?.registered) {
      lines.push(`每周日首次发布：${weekly.schedule_time || '按已设置时间'}。后续触发均为补推检查，仅补发未成功的提醒，已成功则跳过，不会定时重复群发。`);
    }
    if (!schedule.available) lines.push(schedule.error || '计划任务状态暂时未知');
    for (const task of schedule.tasks || []) {
      if (task.name === 'BUCT-Homework-Catchup' && (!task.registered || !task.enabled)) continue;
      const label = task.name === 'BUCT-Homework-Weekly' ? '首次发布及后续补推检查' : '旧版额外补推任务';
      if (!task.registered) { lines.push(`${label}：尚未注册`); continue; }
      const states = {0:'未知',1:'已停用',2:'排队中',3:'就绪',4:'正在运行'};
      lines.push(`${label}：${task.enabled ? (states[task.state] || '未知') : '已停用'}`
        + (task.matches_project ? '' : ' · 注意：任务指向其他位置')
        + (task.enabled ? `\n下次${nextScheduleAction(task)}：${task.next_run || '暂无固定时间'}（届时检查是否需要发送）` : '\n已停用，不会自动运行')
        + ` · 上次系统结果：${task.last_run ? task.last_result === 0 ? '运行完成（0）' : '返回码 ' + task.last_result : '尚未运行'}`);
    }
    const attempt = schedule.last_attempt || {};
    lines.push(attempt.time ? `最近任务记录：${attempt.time} · ${attempt.channel === 'none' ? '演练（未发送）' : attempt.ok ? '运行完成（不等于已发送）' : '运行失败'}`
      + (attempt.error ? '\n原因：' + attempt.error : '') : '最近任务记录：暂无');
    for (const [key,label] of [['class','班级群'],['personal','个人群']]) {
      lines.push(`${label}最近自动发送成功：${schedule.last_success?.[key]?.time || '暂无记录'}`);
    }
    $('#scheduleText').textContent = lines.join('\n');
  } else {
    $('#scheduleText').textContent = $('#scheduleSummary').textContent = '定时任务状态暂时无法读取，请刷新状态。';
    $('#btnScheduleSet').disabled = $('#btnScheduleCancel').disabled = true;
  }
  document.dispatchEvent(new CustomEvent('homework:schedule', { detail: schedule }));
  systemRefreshing = false;
}
$('#btnSystemRefresh').onclick = () => refreshSystemStatus(true);
$('#scheduleDetails').ontoggle = () => {
  $('#scheduleDetails summary').textContent = $('#scheduleDetails').open ? '收起定时状态' : '展开定时状态';
};
$('#btnBackendRefresh').onclick = () => refreshSystemStatus();
function setScheduleEditor(open) {
  const editor = $('#maskSchedule');
  editor.classList.toggle('on', open);
  if (editor instanceof HTMLDialogElement) {
    if (open && !editor.open) editor.showModal();
    if (!open && editor.open) editor.close();
  }
}
$('#btnScheduleSet').onclick = async () => {
  await refreshSystemStatus(true);
  if (!scheduleInfo?.available) return;
  const tasks = scheduleInfo.tasks || [];
  const weekly = tasks.find(t => t.name === 'BUCT-Homework-Weekly');
  $('#scheduleTime').value = weekly?.schedule_time || '09:00';
  $('#scheduleDays').value = weekly?.days || 7;
  $('#scheduleEditStatus').textContent = '';
  setScheduleEditor(true);
};
$('#scheduleClose').onclick = () => { if (!scheduleChanging) setScheduleEditor(false); };
async function changeSchedule(action) {
  if (!scheduleInfo?.available || scheduleChanging) return;
  const time = $('#scheduleTime').value;
  const days = Number($('#scheduleDays').value);
  if (action === 'set' && (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time) || !Number.isInteger(days) || days < 1 || days > 30)) {
    $('#scheduleEditStatus').textContent = '请选择有效时间，提醒天数为1至30天。'; return;
  }
  scheduleChanging = true;
  for (const id of ['scheduleSave','scheduleClose','btnScheduleSet','btnScheduleCancel']) $('#' + id).disabled = true;
  $('#scheduleEditStatus').textContent = $('#scheduleActionStatus').textContent = '正在保存；若弹出Windows权限确认，请允许本项目修改计划任务。';
  const result = await api('/api/schedule', {action, expected_revision:scheduleInfo.revision,
    time, days, catchup:false});
  scheduleChanging = false;
  $('#scheduleSave').disabled = $('#scheduleClose').disabled = false;
  if (result.ok) {
    setScheduleEditor(false);
    $('#scheduleActionStatus').textContent = result.note || '定时设置已更新。';
    log(result.note || '定时设置已更新。');
  } else {
    $('#scheduleEditStatus').textContent = $('#scheduleActionStatus').textContent = '未保存：' + (result.error || '请重试');
    log(result.error || '定时操作失败');
  }
  await refreshSystemStatus(true);
}
$('#scheduleSave').onclick = () => changeSchedule('set');
$('#btnScheduleCancel').onclick = () => {
  if (confirm('取消每周定时发布，并同时停用补推检查？不会中止正在运行的任务。')) changeSchedule('cancel');
};
$('#btnUpdateBackend').onclick = async () => {
  if (!backendInfo || backendUpdating) return;
  if (document.querySelector('.mask.on,dialog[open]:not(#dlgSystem):not(#dlgBackend)')) { log('请先完成或关闭正在编辑的内容，再更新后台。'); return; }
  backendUpdating = true; $('#btnUpdateBackend').disabled = true;
  $('#runtimeText').textContent = '正在更新后台，请稍候…';
  const old = backendInfo;
  const res = await api('/api/runtime/update', {expected_version:old.disk_version, expected_pid:old.pid});
  if (!res.ok) {
    backendUpdating = false; await refreshSystemStatus();
    $('#runtimeText').textContent = '更新未开始：' + (res.error || '连接失败'); return;
  }
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 700));
    const now = await api('/api/runtime');
    if (now.ok && now.pid !== old.pid && !now.needs_update) {
      const draft = {};
      for (const id of updateDraftFields) {
        const el = $('#' + id); if (el) draft[id] = el.type === 'checkbox' ? el.checked : el.value;
      }
      sessionStorage.setItem('homeworkUpdateDraft', JSON.stringify(draft));
      location.reload(); return;
    }
  }
  backendUpdating = false;
  $('#runtimeText').textContent = '后台尚未重新连接，请重新打开控制台；本页输入仍保留。';
};
refreshSystemStatus();
setInterval(() => refreshSystemStatus(), 30000);
