// First deployment and local account/delivery management.
(() => {
  function dialog(id, title, body, footer) {
    const element = document.createElement('dialog');
    element.id = id;
    element.className = 'dlg settings-dialog';
    element.setAttribute('aria-labelledby', id + 'Title');
    element.innerHTML = `<header class="dlg-head"><h3 id="${id}Title">${title}</h3>
      <button class="x" id="${id === 'dlgAccount' ? 'account' : id === 'dlgWecom' ? 'wecom' : 'setup'}Close">关闭</button></header>
      <div class="scroll settings-scroll">${body}</div><footer class="dlg-foot">${footer}</footer>`;
    document.body.append(element);
    element.querySelector('.x').onclick = () => element.close();
    return element;
  }
  const account = dialog('dlgAccount', '学校账号', `
    <p class="settings-help">在当前 Windows 用户下加密保存。登录与自动续登优先使用此账号。已有 Edge 凭据可通过下方按钮导入。</p>
    <form id="accountForm" class="settings-fields">
      <label class="field">学校账号<input id="accountUsername" autocomplete="username" maxlength="160" required></label>
      <label class="field">密码<input id="accountPassword" type="password" autocomplete="new-password" maxlength="4096" placeholder="已保存时留空保留原密码"></label>
      <label class="settings-check"><input id="accountNew" type="checkbox">这是另一个学校账号，建立新的缓存范围</label>
      <p class="settings-help">更换账号后请重新登录、刷新课程表并检索。已有名单可在课程管理中调整。</p>
    </form>
    <p id="accountStatus" role="status"></p>
    <div class="manage-row"><button class="btn line" id="accountImport">导入 Edge 已存凭据</button>
      <button class="btn ghost danger" id="accountClear">移除控制台保存</button></div>
    <div class="manage-row"><button class="btn line" id="accountLogin">重新登录</button>
      <button class="btn ghost" id="accountCheck">检查登录状态</button></div>
    <p class="settings-help">重新登录会打开学校认证窗口。遇到验证码时在该窗口完成，后台会保存成功后的会话。</p>`,
  '<button class="btn solid" id="accountSave" type="submit" form="accountForm">保存账号</button>');
  let accountRevision;
  function showAccount(data, message) {
    accountRevision = data.revision;
    $('#accountUsername').value = data.username;
    $('#accountPassword').value = '';
    $('#accountNew').checked = false;
    $('#accountStatus').textContent = message || data.error ||
      (data.password_saved ? '本机已保存账号密码' : '尚未在控制台保存账号密码') +
      (data.needs_login ? '，请重新登录' : data.session_saved ? '，已有会话，可检查登录状态' : '');
  }
  async function openAccount() {
    openDlg(account);
    showAccount(requireOk(await api('/api/settings/account')));
  }
  async function saveAccount(action) {
    const result = requireOk(await api('/api/settings/account', {
      expected_revision: accountRevision, action,
      username: $('#accountUsername').value, password: $('#accountPassword').value,
      new_account: $('#accountNew').checked,
    }));
    showAccount(result, action === 'clear' ? '已移除控制台保存的凭据，请重新登录' : '已保存，请重新登录以验证凭据');
    await refreshList();
  }
  function operation(element, status, fn) {
    return async event => {
      event?.preventDefault();
      if (element.dataset.busy) return;
      element.dataset.busy = '1';
      const buttons = [...element.querySelectorAll('button')];
      buttons.forEach(button => { button.disabled = true; });
      try { await fn(); }
      catch (error) { status.textContent = error.message; }
      finally {
        delete element.dataset.busy;
        buttons.forEach(button => { button.disabled = false; });
      }
    };
  }
  $('#accountForm').onsubmit = operation(account, $('#accountStatus'), () => saveAccount('save'));
  $('#accountImport').onclick = operation(account, $('#accountStatus'), () => saveAccount('import-edge'));
  $('#accountClear').onclick = operation(account, $('#accountStatus'), () => saveAccount('clear'));
  for (const [button, op] of [['accountLogin', 'login'], ['accountCheck', 'check-login']]) {
    $('#' + button).onclick = operation(account, $('#accountStatus'), async () => {
      $('#accountStatus').textContent = op === 'login' ? '正在登录，请在学校窗口完成验证码…' : '正在检查登录状态…';
      await runTask(op, {});
      showAccount(requireOk(await api('/api/settings/account')), '已验证北化在线登录成功');
    });
  }
  account.addEventListener('close', () => { $('#accountPassword').value = ''; });

  const channels = '<option value="clipboard">复制到剪贴板</option><option value="wecom">企业微信</option>' +
    '<option value="none">仅生成本地文案</option><option value="pushplus">PushPlus（沿用已有配置）</option>' +
    '<option value="wechat">桌面微信（沿用已有配置）</option>';
  const hookFields = [['Main', 'wecom_webhook', '主用地址 · 控制台和定时发布'],
    ['Test', 'wecom_webhook_test', '测试群地址 · 显式测试入口'],
    ['Prod', 'wecom_webhook_prod', '正式群备用地址 · 显式正式入口'],
    ['Personal', 'personal_webhook', '个人完整提醒地址']];
  const wecom = dialog('dlgWecom', '企微推送', `
    <p class="settings-help">可管理已有地址或粘贴新地址。留空保留原地址，勾选清除后删除该项。保存后仅显示配置状态。</p>
    <form id="wecomForm" class="settings-fields">
      <label class="field">班级提醒渠道<select id="wecomChannel">${channels}</select></label>
      ${hookFields.map(([id, key, title]) => `<section class="settings-hook"><label class="field">${title}
        <input id="wecom${id}" type="password" autocomplete="off" placeholder="粘贴完整 Webhook 链接"></label>
        <div class="settings-hook-state"><span id="wecom${id}State"></span>
        <label><input id="wecom${id}Clear" type="checkbox">清除</label></div></section>`).join('')}
      <label class="settings-check"><input id="wecomPersonalEnabled" type="checkbox">启用个人完整提醒</label>
      <label class="field">个人提醒渠道<select id="wecomPersonalChannel">${channels}</select></label>
    </form><p id="wecomStatus" role="status"></p>`,
  '<button class="btn solid" id="wecomSave" type="submit" form="wecomForm">保存推送设置</button>');
  let wecomRevision;
  function showWecom(data, message) {
    wecomRevision = data.revision;
    $('#wecomChannel').value = data.channel;
    $('#wecomPersonalChannel').value = data.personal_channel;
    $('#wecomPersonalEnabled').checked = data.personal_enabled;
    for (const [id, key] of hookFields) {
      $('#wecom' + id).value = '';
      $('#wecom' + id + 'Clear').checked = false;
      $('#wecom' + id + 'State').textContent = data.configured[key] ? '已配置' : '未配置';
    }
    $('#wecomStatus').textContent = message || (data.environment_override ? '当前存在环境变量地址覆盖，发送时优先使用该覆盖地址。' : '');
  }
  async function openWecom() {
    openDlg(wecom);
    showWecom(requireOk(await api('/api/settings/wecom')));
  }
  $('#wecomForm').onsubmit = operation(wecom, $('#wecomStatus'), async () => {
    const payload = { expected_revision: wecomRevision, channel: $('#wecomChannel').value,
      personal_channel: $('#wecomPersonalChannel').value, personal_enabled: $('#wecomPersonalEnabled').checked };
    for (const [id, key] of hookFields) {
      payload[key] = $('#wecom' + id).value;
      payload['clear_' + key] = $('#wecom' + id + 'Clear').checked;
    }
    const result = requireOk(await api('/api/settings/wecom', payload));
    state.channel = result.channel;
    showWecom(result, '已保存推送设置');
  });
  wecom.addEventListener('close', () => hookFields.forEach(([id]) => { $('#wecom' + id).value = ''; }));

  const setup = dialog('dlgSetup', '首次部署与使用指引', `
    <p class="settings-help">依次完成环境检查、学校登录、推送设置和课程初始化。此指引可随时从管理菜单打开。</p>
    <ol class="setup-steps">
      <li><h4>检查运行环境</h4><p id="setupEnvironment"></p><p id="setupPython" class="settings-path"></p></li>
      <li><h4>保存学校账号并登录</h4><p id="setupAccountState"></p><button class="btn line" id="setupAccount">打开学校账号</button></li>
      <li><h4>配置提醒渠道</h4><p id="setupWecomState"></p><button class="btn line" id="setupWecom">打开企微推送</button></li>
      <li><h4>刷新课程表</h4><p id="setupCoursesState"></p><button class="btn line" id="setupCourses">刷新课程表</button></li>
      <li><h4>检索、预览和定时发布</h4><p>关闭指引后点“检索北化在线”，检查列表和两份本地预览。确认提醒范围后，可从管理菜单设置定时发布。</p></li>
    </ol><p id="setupStatus" role="status"></p>`,
  '<button class="btn solid" id="setupFinish">完成指引</button>');
  let setupRevision;
  function showSetup(data) {
    setupRevision = data.revision;
    $('#setupEnvironment').textContent = `Python ${data.python} · Playwright ${data.playwright ? '已安装' : '缺失'} · Pillow ${data.pillow ? '已安装' : '缺失'} · Edge ${data.edge ? '已安装' : '未找到'}`;
    $('#setupPython').textContent = data.python_path;
    $('#setupAccountState').textContent = data.account.password_saved ? '已保存账号。请在账号面板检查登录状态或重新登录。' : '可录入账号密码，或导入已有 Edge 凭据后登录。';
    $('#setupWecomState').textContent = `当前渠道为 ${data.wecom.channel}，主用企微地址${data.wecom.configured.wecom_webhook ? '已配置' : '未配置'}。`;
    $('#setupCoursesState').textContent = `本账号课程表 ${data.course_count} 门。首次登录或换账号后请刷新。`;
  }
  async function openSetup() {
    openDlg(setup);
    showSetup(requireOk(await api('/api/setup')));
  }
  $('#setupAccount').onclick = guarded(async () => { setup.close(); await openAccount(); });
  $('#setupWecom').onclick = guarded(async () => { setup.close(); await openWecom(); });
  $('#setupCourses').onclick = operation(setup, $('#setupStatus'), async () => {
    $('#setupStatus').textContent = '正在刷新课程表…';
    await runTask('refresh-courses', {});
    await loadCourses();
    showSetup(requireOk(await api('/api/setup')));
    $('#setupStatus').textContent = '课程表已刷新';
  });
  $('#setupFinish').onclick = operation(setup, $('#setupStatus'), async () => {
    requireOk(await api('/api/setup', { expected_revision: setupRevision }));
    setup.close();
  });
  $('#openAccount').onclick = guarded(openAccount);
  $('#openWecom').onclick = guarded(openWecom);
  $('#openSetup').onclick = guarded(openSetup);
  (async () => {
    try {
      const data = requireOk(await api('/api/setup'));
      if (data.needs_setup) { showSetup(data); openDlg(setup); }
    } catch (error) { report(error); }
  })();
})();
