/* Shared persistent activity, refreshed only while this dialog is open. */
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'dlgLogs';
  dialog.className = 'dlg run-dialog';
  dialog.setAttribute('aria-labelledby', 'runsTitle');
  dialog.innerHTML = `<header class="dlg-head"><h3 id="runsTitle">运行记录</h3><button class="x" id="logsClose">关闭</button></header>
    <div class="runs-toolbar"><span id="runsStatus" role="status">正在读取…</span><button class="btn sm line" id="runsRefresh">刷新记录</button></div>
    <div class="scroll runs-scroll" id="runsScroll">
      <section><h4>正在进行</h4><div id="runActive"></div></section>
      <section><h4>历史记录</h4><p class="runs-help">跨页面和后台重启保留。展开查看进度与错误，旧任务按已有台账恢复。</p><div id="runHistory"></div><button class="btn sm line" id="runsMore" hidden>加载更早记录</button></section>
      <details class="runs-local"><summary>本页界面提示</summary><pre id="logText"></pre></details>
    </div>`;
  document.body.append(dialog);
  const statuses = {queued:'排队中', running:'运行中', done:'已完成', partial:'部分完成', error:'失败', cancelled:'已取消'};
  const history = new Map();
  let timer, cursor, paged, busy = false, generation = 0;
  const stamp = value => value ? new Date(value*1000).toLocaleString('zh-CN', {hour12:false}) : '—';
  const seconds = value => Number.isFinite(value) ? value.toFixed(1)+' 秒' : '—';

  function card(task) {
    const progress = task.progress;
    const summary = task.summary || {};
    const events = task.events || [];
    const active = ['queued','running'].includes(task.status);
    const coverage = progress ? `已完成 ${progress.done}/${progress.total} 门课程${progress.failed ? `，失败 ${progress.failed} 门` : ''}` : '';
    const counts = Number.isFinite(summary.platform_count) ? `平台条目 ${summary.platform_count} 条` : '';
    const error = summary.error || progress?.error || '';
    const detail = events.map(event => `${stamp(event.at)}  ${event.message}`).join('\n');
    const courses = progress ? [
      progress.completed_courses?.length ? '已完成课程 · '+progress.completed_courses.join('、') : '',
      progress.failed_courses?.length ? '失败课程 · '+progress.failed_courses.join('、') : '',
    ].filter(Boolean).join('\n') : '';
    return `<article class="run-card ${active ? 'is-active' : ''}" data-run="${esc(task.task_id)}">
      <div class="run-heading"><strong>${esc(taskLabels[task.op] || task.op)}</strong><span class="run-badge ${esc(task.status)}">${esc(statuses[task.status] || task.status)}</span></div>
      <div class="run-meta">${esc(stamp(task.created_at))} · ${task.status === 'queued' ? '等待' : '用时'} ${esc(seconds(task.elapsed_sec))}${task.queue_sec >= 1 && task.status !== 'queued' ? ' · 排队 '+esc(seconds(task.queue_sec)) : ''}</div>
      ${coverage || counts ? `<p>${esc([coverage,counts].filter(Boolean).join(' · '))}</p>` : ''}
      ${error ? `<p class="run-error">${esc(error)}</p>` : ''}
      ${active && events.length ? `<p class="run-current">${esc(events.at(-1).message)}</p>` : ''}
      <details class="run-details" ${active ? 'open' : ''}><summary>查看过程与结果</summary>
      <pre>${esc([courses,detail].filter(Boolean).join('\n\n') || (active ? '正在执行，等待下一条进度…' : '此历史任务仅保留执行结果。安装后的任务会记录详细进度。'))}</pre>
      <small>任务 ${esc(task.task_id)}${events.length >= 100 ? ' · 展示最近 100 行过程' : ''}</small></details>
    </article>`;
  }

  function render(selector, tasks, empty) {
    const container = $(selector);
    const expanded = new Map([...container.querySelectorAll('[data-run]')].map(row => [row.dataset.run, row.querySelector('details').open]));
    const scrolls = new Map([...container.querySelectorAll('[data-run]')].map(row => [row.dataset.run, row.querySelector('pre').scrollTop]));
    container.innerHTML = tasks.length ? tasks.map(card).join('') : `<p class="runs-empty">${empty}</p>`;
    container.querySelectorAll('[data-run]').forEach(row => {
      if (expanded.has(row.dataset.run)) row.querySelector('details').open = expanded.get(row.dataset.run);
      row.querySelector('pre').scrollTop = scrolls.get(row.dataset.run) || 0;
    });
  }

  async function refresh(older = false) {
    if (busy || !dialog.open) return;
    const ticket = generation;
    busy = true;
    $('#runsRefresh').disabled = $('#runsMore').disabled = true;
    try {
      const result = requireOk(await api('/api/runs?limit=50'+(older && cursor ? '&before='+encodeURIComponent(cursor) : '')));
      if (ticket !== generation || !dialog.open) return;
      result.history.forEach(task => history.set(task.task_id, task));
      result.active.forEach(task => history.delete(task.task_id));
      if (older || !paged) cursor = result.next_cursor;
      if (older) paged = true;
      const position = $('#runsScroll').scrollTop;
      render('#runActive', result.active, '当前没有正在进行的任务');
      render('#runHistory', [...history.values()].sort((a,b) => b.created_at-a.created_at), '暂无历史记录');
      $('#runsScroll').scrollTop = position;
      $('#runsMore').hidden = !cursor;
      $('#runsStatus').textContent = `每 1.5 秒自动刷新 · 更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12:false})}`;
    } catch (error) {
      if (ticket === generation) $('#runsStatus').textContent = '读取失败 · '+error.message+'，可点击刷新重试';
    } finally {
      busy = false;
      $('#runsRefresh').disabled = $('#runsMore').disabled = false;
      clearTimeout(timer);
      if (dialog.open) timer = setTimeout(refresh, 1500);
    }
  }

  $('#openLogs').onclick = () => {
    generation++;
    history.clear(); cursor = null; paged = false;
    $('#logText').textContent = state.logs.join('\n') || '本页暂无界面提示';
    openDlg(dialog);
    refresh();
  };
  $('#logsClose').onclick = () => dialog.close();
  dialog.addEventListener('close', () => { generation++; clearTimeout(timer); });
  $('#runsRefresh').onclick = () => refresh();
  $('#runsMore').onclick = () => refresh(true);
})();
