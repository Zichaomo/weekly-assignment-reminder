/* Local course content rule editor. Preview never fetches or sends. */
(() => {
  const el = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const button = document.createElement('button');
  button.id = 'btnCourseRules'; button.className = 'btn ghost'; button.textContent = '课程正文规则';
  el('btnBlCourse').before(button);
  const style = document.createElement('style');
  style.textContent = `#ruleMask .modal{width:min(1100px,96vw);max-height:94vh}#ruleMask .rule-layout{display:grid;grid-template-columns:310px 1fr;gap:24px}#ruleMask .rule-list{max-height:450px;overflow:auto}#ruleMask .rule-row{display:block;width:100%;text-align:left;padding:14px;margin:6px 0;border:1px solid var(--line,#ddd);background:transparent;border-radius:6px;cursor:pointer;color:inherit}#ruleMask .rule-row.active{border-color:var(--accent,#9c4334);background:#f7eee5}#ruleMask small{display:block;color:#777;margin-top:6px}#ruleMask label{display:block;margin:12px 0 5px}#ruleMask input,#ruleMask select,#ruleMask textarea{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccc;border-radius:4px;background:var(--paper,#fff);color:inherit;font:inherit}#ruleMask pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f2ec;padding:14px;min-height:70px;font:inherit}#ruleMask .rule-options{display:grid;grid-template-columns:1fr 1fr;gap:14px}#ruleMask .rule-status{min-height:24px;color:#945132;margin:12px 0} @media(max-width:720px){#ruleMask .rule-layout{grid-template-columns:1fr}#ruleMask .rule-list{max-height:180px}}`;
  document.head.append(style);
  const mask = document.createElement('div'); mask.id = 'ruleMask'; mask.className = 'mask';
  mask.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="ruleTitle">
    <header><h3 id="ruleTitle">课程正文与推送规则</h3><button id="ruleClose" class="btn small ghost">关闭</button></header>
    <div class="body"><p>按课程选择正文处理方式。未匹配课程自动识别题号与任务；无法识别时提示查看北化在线。标题与截止时间照常保留。</p>
    <div class="rule-layout"><aside><input id="ruleSearch" aria-label="搜索课程规则" placeholder="搜索课程规则…"><div id="ruleList" class="rule-list"></div><button id="ruleNew" class="btn ghost">＋ 新增课程规则</button><small>精确匹配优先，其次采用名称最长的前缀规则。</small></aside>
    <section><label for="ruleCourse">课程名称</label><input id="ruleCourse" list="ruleCourses" maxlength="160" placeholder="选择或输入完整课程名称"><datalist id="ruleCourses"></datalist>
    <div class="rule-options"><div><label for="ruleMatch">匹配方式</label><select id="ruleMatch"><option value="exact">名称完全相同</option><option value="prefix">课程名以此开头</option></select></div><div><label for="ruleLines">群消息正文最多显示几行</label><input id="ruleLines" type="number" min="1" max="20" value="3"></div></div>
    <label for="ruleMode">正文处理方式</label><select id="ruleMode"></select><small id="ruleHelp"></small>
    <label for="ruleSample">用已缓存作业试用</label><select id="ruleSample"><option value="">粘贴文字试用</option></select>
    <textarea id="ruleText" rows="4" aria-label="试用正文" placeholder="粘贴平台正文，查看处理结果；不会保存作业正文"></textarea>
    <button id="rulePreview" class="btn ghost" style="margin-top:10px">预览当前草稿</button><small>仅使用本地缓存或粘贴文字，不检索平台、不发送消息。作业的人工正文确认优先于文字/原图策略；仅提示策略始终生效。</small>
    <pre id="ruleResult">选择规则后可先预览再保存。</pre></section></div><div id="ruleStatus" class="rule-status" role="status"></div></div>
    <footer><button id="ruleReset" class="btn ghost">恢复默认</button><button id="ruleSave" class="btn accent">保存规则</button></footer></div>`;
  document.body.append(mask);
  let state, current, samples = [], dirty = false, busy = false;
  const help = {auto:'仅提取能明确识别的任务；遇到不确定要求时提示查看平台。',reference:'不附正文或原图，统一提示“详细要求见北化在线”。',physics:'去除已知通用提交套话，保留其余文字；含图片、表格或附件时采用保守识别。',full:'保留全部纯文字，只清理行边空白和空行；含图片、表格或附件时采用保守识别。',images:'正文图片均已缓存时随提醒发送原图；否则采用保守识别。'};
  async function request(url, data) {
    const response = await fetch(url, data ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)} : {});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '请求失败');
    return result;
  }
  function status(text) { el('ruleStatus').textContent = text; }
  function renderList() {
    const query = el('ruleSearch').value.trim();
    el('ruleList').innerHTML = state.rules.filter(r => r.course.includes(query)).map(r => `<button class="rule-row ${r.id === current?.id ? 'active' : ''}" data-rule="${escape(r.id)}"><b>${escape(r.course)}</b><small>${escape(state.modes[r.mode])} · ${r.lines} 行 · ${r.customized ? '已自定义' : '内置默认'}</small></button>`).join('') || '<p>暂无匹配规则</p>';
    el('ruleList').querySelectorAll('[data-rule]').forEach(b => b.onclick = () => {
      if (leave()) select(state.rules.find(r => r.id === b.dataset.rule));
    });
  }
  function leave() { return !dirty || confirm('当前规则有未保存的修改，放弃修改？'); }
  function select(rule) {
    current = rule; dirty = false;
    el('ruleCourse').value = rule.course; el('ruleMatch').value = rule.match;
    el('ruleMode').value = rule.mode; el('ruleLines').value = rule.lines;
    el('ruleReset').textContent = rule.builtin ? '恢复内置默认' : '删除自定义规则';
    el('ruleReset').disabled = !rule.customized;
    el('ruleText').value = ''; el('ruleText').hidden = false;
    el('ruleResult').textContent = '选择样本或粘贴正文，预览当前草稿。'; status('');
    updateSamples(); renderList();
  }
  function updateSamples() {
    const name = el('ruleCourse').value.trim(), prefix = el('ruleMatch').value === 'prefix';
    el('ruleSample').innerHTML = '<option value="">粘贴文字试用</option>' + samples.filter(s => s.course === name || (prefix && s.course?.startsWith(name))).map(s => `<option value="${escape(s.item_key)}">${escape(s.course)}｜${escape(s.title)}</option>`).join('');
    el('ruleText').hidden = false; el('ruleHelp').textContent = help[el('ruleMode').value];
  }
  function draft() { return {id:current.id,course:el('ruleCourse').value.trim(),match:el('ruleMatch').value,mode:el('ruleMode').value,lines:Number(el('ruleLines').value)}; }
  async function perform(fn) {
    if (busy) return; busy = true;
    for (const id of ['ruleSave','rulePreview','ruleNew','ruleClose','ruleReset']) el(id).disabled = true;
    try { await fn(); } catch (e) { status(e.message); }
    finally { busy = false; for (const id of ['ruleSave','rulePreview','ruleNew','ruleClose']) el(id).disabled = false; el('ruleReset').disabled = !current?.customized; }
  }
  button.onclick = () => perform(async () => {
    const [rules, local, courses] = await Promise.all([request('/api/course-rules'),request('/api/course-rules/samples'),request('/api/courses')]);
    state = rules; samples = local.items;
    el('ruleMode').innerHTML = Object.entries(state.modes).map(([key,label]) => `<option value="${key}">${escape(label)}</option>`).join('');
    el('ruleCourses').innerHTML = (courses.courses || []).map(c => `<option value="${escape(c.name)}"></option>`).join('');
    el('ruleSearch').value = ''; select(state.rules[0]); mask.classList.add('on'); el('ruleCourse').focus();
  });
  el('ruleSearch').oninput = renderList;
  el('ruleNew').onclick = () => { if (leave()) select({id:crypto.randomUUID(),course:'',match:'exact',mode:'auto',lines:3}); };
  el('ruleClose').onclick = () => { if (leave()) { mask.classList.remove('on'); button.focus(); } };
  mask.addEventListener('keydown', e => { if (e.key === 'Escape' && !busy) el('ruleClose').click(); });
  for (const id of ['ruleCourse','ruleMatch','ruleMode','ruleLines']) el(id).addEventListener('input', () => { dirty = true; updateSamples(); el('ruleResult').textContent = '草稿已修改，请重新预览。'; });
  el('ruleSample').onchange = () => { el('ruleText').hidden = !!el('ruleSample').value; };
  el('rulePreview').onclick = () => perform(async () => {
    const result = await request('/api/course-rules/preview',{rule:draft(),item_key:el('ruleSample').value,text:el('ruleText').value});
    el('ruleResult').textContent = `群消息正文预览\n${result.push_text || '（仅保留标题与截止时间）'}\n\n处理说明：${result.reason}`;
    status('这是当前草稿的试用结果，尚未保存。');
  });
  el('ruleSave').onclick = () => perform(async () => {
    const rule = draft(); state = await request('/api/course-rules',{action:'save',expected_revision:state.revision,rule});
    select(state.rules.find(r => r.id === rule.id)); status('已保存。后续预览与推送使用新规则，无需重新抓取；已发送的消息不变。');
  });
  el('ruleReset').onclick = () => perform(async () => {
    if (!confirm(current.builtin ? '恢复这条课程规则的内置默认设置？' : '删除这条自定义规则，恢复自动匹配？')) return;
    const id = current.id;
    state = await request('/api/course-rules',{action:'reset',id,expected_revision:state.revision});
    select(state.rules.find(r => r.id === id) || state.rules[0]); status('已恢复规则。');
  });
})();
