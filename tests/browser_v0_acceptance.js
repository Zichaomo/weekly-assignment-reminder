// Acceptance criteria written before the fixes for the 2026-11-09 screenshots.
// Uses an isolated backend and permits only its exact loopback origin.
const assert = require('assert/strict');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require(process.env.PLAYWRIGHT_PATH ||
  'C:/Users/MECHREVO/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.js');

const root = path.resolve(__dirname, '..');
const output = path.join(__dirname, 'artifacts', 'v0-fidelity');
const failures = [];
const checks = [];
const externalRequests = [];
const pageErrors = [];
const taskOperations = [];
let fixture;
let browser;
let timeout;

async function check(name, run) {
  try {
    await run();
    checks.push({ name, ok: true });
  } catch (error) {
    failures.push(name);
    checks.push({ name, ok: false, error: error.message });
  }
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  timeout = setTimeout(() => {
    fixture?.kill();
    browser?.close();
    process.exitCode = 1;
  }, 90000);
  const python = process.env.PYTHON || path.join(os.homedir(),
    '.workbuddy/binaries/python/envs/default/Scripts/python.exe');
  fixture = spawn(python, ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    cwd: root,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', HOMEWORK_PUBLISH_FRESH_SEC: '-1' },
    windowsHide: true,
  });
  const info = await new Promise((resolve, reject) => {
    let text = '';
    let errors = '';
    const deadline = setTimeout(() => reject(new Error('Fixture timeout ' + errors)), 15000);
    fixture.stderr.on('data', data => { errors += data; });
    fixture.stdout.on('data', data => {
      text += data;
      const line = text.split(/\r?\n/).find(value => value.startsWith('{'));
      if (line) {
        clearTimeout(deadline);
        resolve(JSON.parse(line));
      }
    });
    fixture.on('exit', () => reject(new Error(errors || 'Fixture exited')));
  });
  const origin = 'http://127.0.0.1:' + info.port;
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
  });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin !== origin) {
      externalRequests.push(route.request().url());
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/tasks') {
      taskOperations.push(request.postDataJSON().op);
    }
  });

  // First reproduce the actual failure through the real preview queue.
  await page.goto(origin);
  await page.waitForSelector('#list .item');
  await page.locator('#pvBtn').click();
  await page.waitForFunction(() => state.preview !== null || state.logs.some(line => line.includes('部分失败')));
  await check('stale cache preview renders both messages without a JSON error toast', async () => {
    assert.match(await page.locator('#bubble').innerText(), /作业提醒/);
    assert.doesNotMatch(await page.locator('#toast').innerText(), /部分失败|"coverage"|"message"|"marker_hash"/);
    assert.equal(await page.locator('#publish').isDisabled(), true);
    await page.locator('#seg [data-v="personal"]').click();
    assert.match(await page.locator('#bubble').innerText(), /我的作业提醒/);
    assert.match(await page.locator('#previewNotice').innerText(), /缓存|预览/);
  });
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(output, 'preview-stale.png') });
  await page.locator('#pvClose').click();
  await check('an actual failed preview is still rejected and leaves a retryable message', async () => {
    await page.route('**/api/tasks', route => route.fulfill({ json: {
      ok: true, task_id: 'fixture-failed-preview',
    } }));
    await page.route('**/api/tasks/fixture-failed-preview', route => route.fulfill({ json: {
      ok: true, status: 'partial', result: {
        ok: false, error: 'fixture preview failure', coverage: { publishable: false },
      },
    } }));
    try {
      await page.locator('#pvBtn').click();
      await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('fixture preview failure'));
      assert.match(await page.locator('#bubble').innerText(), /失败/);
      assert.equal(await page.locator('#publish').isDisabled(), true);
      assert.equal(await page.locator('#copyBtn').isDisabled(), true);
    } finally {
      await page.locator('#pvClose').click();
      await page.unroute('**/api/tasks');
      await page.unroute('**/api/tasks/fixture-failed-preview');
    }
  });

  // Fixed API snapshot provides the same facts for all visual assertions.
  await page.clock.setFixedTime(new Date('2026-11-09T10:00:00+08:00'));
  const response = await page.request.get(origin + '/api/list');
  const snapshot = await response.json();
  const primary = {
    ...snapshot.rows.find(row => row.item_key === info.item_key),
    deadline: '2026-11-09 23:59', submit_state: 'submitted', elective: false,
    content_checked_at: '2026-11-09T08:45:00+0800',
    content_text: '',
    content_blocks: [{ type: 'image', asset_id: snapshot.rows[0].asset_ids[0], status: 'ready' }],
    asset_ids: [snapshot.rows[0].asset_ids[0]],
    content_summary: { status: 'images', text: '', reason: '本课程正文图片随提醒发送；有有效裁剪时使用裁剪图' },
  };
  const rows = [primary,
    { ...primary, item_key: 'fixture-tomorrow', title: '明天的练习', deadline: '2026-11-10 18:30',
      kind: 'test', elective: true, submit_state: 'unsubmitted' },
    { source: '手动', item_key: 'manual:100', manual_id: 100, course: '课堂补录',
      title: '稍后截止', deadline: '2026-11-12 20:00', note: '真实补录备注' },
    { source: '手动', item_key: 'manual:101', manual_id: 101, course: '课堂补录',
      title: '过期手动任务', deadline: '2026-11-08 20:00', note: '' },
    { source: '手动', item_key: 'manual:102', manual_id: 102, course: '课堂补录',
      title: '范围外手动任务', deadline: '2026-11-22 20:00', note: '' },
  ];
  const updated = '2026-11-09T08:45:00+0800';
  let scheduleEnabled = true;
  let scheduleAvailable = true;
  await page.route('**/api/list?*', route => route.fulfill({ json: {
    ...snapshot, rows, data_fetched_at: updated,
    cache: { ...snapshot.cache, updated_at: updated, publishable: false,
      publish_block_reasons: ['缓存已超过 300 秒新鲜度要求'] },
  } }));
  await page.route('**/api/cache', route => route.fulfill({ json: {
    ok: true, item_count: 123, content_ready: 2,
    coverage: { updated_at: updated, publishable: false },
  } }));
  await page.route(/\/api\/schedule(?:\?.*)?$/, route => route.fulfill({ json: {
    ok: true, available: scheduleAvailable, revision: 'fixture-revision',
    tasks: [{ name: 'BUCT-Homework-Weekly', registered: true, enabled: scheduleEnabled,
      schedule_time: '18:30', next_run: '2026-11-15 18:30:00', state: 3, matches_project: true }],
    last_attempt: {}, last_success: {},
  } }));
  await page.reload();
  await page.waitForSelector('#list .item');
  await page.waitForFunction(() => !systemRefreshing);

  await check('course rules menu entry has its local file icon', async () => {
    await page.locator('#menuBtn').click();
    try {
      assert.equal(await page.locator('#btnCourseRules svg').count(),1);
      assert.equal(await page.locator('#btnCourseRules svg').isVisible(),true);
    } finally {
      await page.locator('#menuBtn').click();
    }
  });
  await check('schedule and backend version have separate menu entries and panels', async () => {
    await page.locator('#menuBtn').click();
    try {
      assert.equal(await page.locator('#openSystem').innerText(),'定时发布');
      assert.equal(await page.locator('#openBackend').innerText(),'后台版本');
      await page.locator('#openSystem').click();
      assert.equal(await page.locator('#dlgSystem #runtimeText').count(),0);
      assert.equal(await page.locator('#dlgSystem #scheduleSummary').isVisible(),true);
      await page.locator('#systemClose').click();
      await page.locator('#menuBtn').click();
      await page.locator('#openBackend').click();
      assert.equal(await page.locator('#dlgBackend #runtimeText').isVisible(),true);
      assert.equal(await page.locator('#dlgBackend #scheduleSummary').count(),0);
      await page.waitForFunction(()=>!systemRefreshing);
      await page.route('**/api/runtime',route=>route.fulfill({json:{ok:true,needs_update:true,busy:false,pid:77,disk_version:'fixture-new',running_version:'fixture-old'}}));
      await page.route('**/api/runtime/update',route=>route.fulfill({status:409,json:{ok:false,error:'fixture reload rejected'}}));
      await page.evaluate(()=>refreshSystemStatus(true));
      await page.locator('#btnUpdateBackend').click();
      await page.waitForFunction(()=>document.querySelector('#runtimeText').textContent.includes('fixture reload rejected'));
      await page.locator('#backendClose').click();
    } finally {
      await page.unroute('**/api/runtime');
      await page.unroute('**/api/runtime/update');
      await page.evaluate(()=>{document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());closeAllPops();});
    }
  });

  await check('titlebar retains live update time, status dot and configured weekly time', async () => {
    const status = await page.locator('.titlebar .status').innerText();
    assert.match(status, /数据更新于.*08:45/s);
    assert.match(status, /每周日.*18:30.*自动提醒/s);
    assert.equal(await page.locator('.status .dot').count(), 1);
  });
  await check('schedule title reflects disabled and unreadable states', async () => {
    scheduleEnabled = false;
    await page.evaluate(() => refreshSystemStatus(true));
    assert.match(await page.locator('.titlebar .status').innerText(), /定时.*未开启|定时.*停用/);
    scheduleAvailable = false;
    await page.evaluate(() => refreshSystemStatus(true));
    assert.match(await page.locator('.titlebar .status').innerText(), /定时.*无法读取|定时.*未知/);
    scheduleEnabled = scheduleAvailable = true;
    await page.evaluate(() => refreshSystemStatus(true));
  });
  await check('filters retain the olive day pill and switch tracks', async () => {
    assert.equal(await page.locator('.chip.toggle .sw').count(), 2);
    assert.equal(await page.locator('.chips .chip').first().getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('.chips .chip').first().evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(59, 74, 45)');
    const visibleCheckboxes = await page.locator('.chip input[type="checkbox"]').evaluateAll(inputs =>
      inputs.filter(input => getComputedStyle(input).opacity !== '0' && getComputedStyle(input).appearance !== 'none').length);
    assert.equal(visibleCheckboxes, 0);
  });
  await check('list groups, badges and complete statistics follow the prototype', async () => {
    const headings = await page.locator('#list .group-h').allTextContents();
    assert(headings.some(text => text.includes('今天')));
    assert(headings.some(text => text.includes('明天')));
    assert(headings.some(text => text.includes('本周稍后')));
    assert(headings.some(text => text.includes('本周稍后') && text.includes('11月12日')));
    assert.equal(await page.locator('#list .group-h .tag-hot').count(), 1);
    const stats = await page.locator('#stats').innerText();
    assert.match(stats, /明天截止/);
    assert.match(stats, /选修/);
    assert.match(await page.locator('#list .src.platform').first().innerText(), /平台作业/);
    assert.match(await page.locator('#list .src.manual').first().innerText(), /手动补录/);
  });
  await check('default date filter hides overdue and outside-window manual tasks', async () => {
    const list = await page.locator('#list').innerText();
    assert.doesNotMatch(list, /过期手动任务|范围外手动任务/);
    await page.locator('#chkAll').check();
    await page.waitForFunction(() => document.querySelector('#list').textContent.includes('过期手动任务'));
    await page.locator('#chkAll').uncheck();
    await page.waitForFunction(() => !document.querySelector('#list').textContent.includes('过期手动任务'));
  });
  await page.locator('#list .item').filter({ hasText: primary.title }).click();
  await check('deadline has the three-column timeline, minute countdown and urgency color', async () => {
    assert.equal(await page.locator('.deadline .timeline').count(), 1);
    assert.equal(await page.locator('.deadline .timeline').isVisible(), true);
    assert.match(await page.locator('.deadline').innerText(), /11月9日.*周一.*23:59/s);
    assert.match(await page.locator('.count .k').innerText(), /今天截止/);
    assert.match(await page.locator('.count .v').innerText(), /13\s*小时\s*59\s*分/);
    assert.equal(await page.locator('.count .v').evaluate(element => getComputedStyle(element).color), 'rgb(177, 83, 47)');
    const fraction = await page.locator('.timeline .bar').evaluate(element =>
      element.firstElementChild.getBoundingClientRect().width / element.getBoundingClientRect().width);
    assert(Math.abs(fraction - (1 - (13 * 60 + 59) / (7 * 24 * 60))) < .02);
    assert.match(await page.locator('.timeline .scale').innerText(), /现在.*11月9日.*10:00.*7\s*天后/s);
  });
  await check('detail restores audience badges, source time and the image section without an empty origin card', async () => {
    assert.equal(await page.locator('.flags .done').count(), 1);
    assert.match(await page.locator('.flags').innerText(), /进入班级提醒.*进入个人提醒/s);
    assert.match(await page.locator('#detail').innerText(), /抓取于.*08:45/s);
    assert.match(await page.locator('#itemImages .sec-h').innerText(), /作业图片.*1\s*张/s);
    assert.equal(await page.locator('#origin').isVisible(), false);
    assert.doesNotMatch(await page.locator('.summary').innerText(), /本课程正文图片|原图策略|选做/);
  });
  await check('desktop proportions retain the original 470px list and 44px detail inset', async () => {
    const width = await page.locator('.list-pane').evaluate(element => element.getBoundingClientRect().width);
    assert(Math.abs(width - 470) < 1, 'list width ' + width);
    assert.equal(await page.locator('#detail').evaluate(element => getComputedStyle(element).paddingLeft), '44px');
  });
  await check('viewport boundary fix survives restored desktop details', async () => {
    for (const [width, height] of [[1280,800], [1440,900], [1100,600]]) {
      await page.setViewportSize({ width, height });
      const shell = await page.locator('.window').boundingBox();
      assert(shell.y >= 0 && shell.y + shell.height <= height + 1);
      assert(await page.locator('.deadline .timeline').isVisible());
    }
  });
  await check('countdown updates on return without reloading body images', async () => {
    const image = await page.locator('#itemImages img').elementHandle();
    await page.clock.setFixedTime(new Date('2026-11-09T10:05:00+08:00'));
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.match(await page.locator('.count .v').innerText(), /13\s*小时\s*54\s*分/);
    assert.equal(await image.evaluate(element => element.isConnected), true);
    await page.clock.setFixedTime(new Date('2026-11-09T10:00:00+08:00'));
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  });
  await check('restored filter drafts keep their switch appearance in sync', async () => {
    await page.evaluate(() => {
      sessionStorage.setItem('homeworkUpdateDraft', JSON.stringify({chkTests:false, chkAll:true, numDays:5}));
    });
    await page.reload();
    await page.waitForSelector('#list .item');
    assert.equal(await page.locator('#chkTests').isChecked(), false);
    assert.equal(await page.locator('#chkTests').evaluate(element => element.closest('.chip').getAttribute('aria-pressed')), 'false');
    assert.equal(await page.locator('#chkAll').evaluate(element => element.closest('.chip').getAttribute('aria-pressed')), 'true');
    await page.locator('#chkAll').uncheck();
    await page.locator('#chkTests').check();
    await page.locator('#numDays').fill('7');
    await page.locator('#numDays').dispatchEvent('change');
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.evaluate(() => refreshList());
  await page.waitForTimeout(350);
  await page.screenshot({ path: path.join(output, 'home.png') });
  await check('manual date uses the explicit yyyy/mm/dd hint and normalizes calendar input', async () => {
    const submissions = [];
    await page.route('**/api/manual', route => {
      submissions.push(route.request().postDataJSON());
      return route.fulfill({ json: { ok: true } });
    });
    await page.locator('#addBtn').click();
    try {
      assert.equal(await page.locator('#fDate').getAttribute('placeholder'), 'yyyy/mm/dd');
      await page.screenshot({ path: path.join(output, 'date-input.png') });
      await page.locator('#fCourse').fill('补录课程');
      await page.locator('#fTitle').fill('日期测试');
      await page.locator('#fDate').fill('2026/02/29');
      await page.locator('#hwForm [type="submit"]').click();
      assert.equal(submissions.length, 0, 'invalid calendar day must not submit');
      await page.locator('#fDatePicker').evaluate(input => {
        input.value = '2028-02-29';
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });
      assert.equal(await page.locator('#fDate').inputValue(), '2028/02/29');
      await page.locator('#fTime').fill('18:20');
      await page.locator('#hwForm [type="submit"]').click();
      await page.waitForFunction(() => !document.querySelector('#dlgForm').open);
      assert.equal(submissions[0].deadline, '2028-02-29 18:20');
    } finally {
      await page.locator('#dlgForm').evaluate(dialog => dialog.close());
      await page.unroute('**/api/manual');
    }
  });
  await check('preview places each image at its original message marker before the next item and footer', async () => {
    await page.evaluate(assetId => {
      const message = '📚 作业提醒 · 未来 7 天（至 11-15）\n\n⚠️ 近两天截止\n'
        + '• 英语 📌手动｜U校园 新视野U5\n   ⏰ 今天 23:59 截止\n'
        + '• 复变函数与积分变换（A）｜图片练习\n   ⏰ 明天 23:59 截止\n'
        + '   作业原图随提醒发送（1 张）。\n'
        + '• 另一门课｜下一项练习\n   ⏰ 明天 23:59 截止\n'
        + '   作业图片随提醒发送（1 张）。\n\n—— 共 3 项，详情以「北化在线」为准';
      const images = [
        { item_key: 'picture-a', course: '复变函数与积分变换（A）', title: '图片练习', asset_id: assetId },
        { item_key: 'picture-b', course: '另一门课', title: '下一项练习', asset_id: assetId, delivery_asset_id: assetId, crop: true },
      ];
      state.target = 'class';
      state.preview = { ok: true, message, message_personal: message.replace('作业提醒', '我的作业提醒（含选修课）'),
        count: 3, count_personal: 3, images, images_personal: images, cache_blocked: true };
      renderPreview();
      setPop(pvBtn, pv, true);
    }, primary.asset_ids[0]);
    try {
      assert.doesNotMatch(await page.locator('#bubble').innerText(), /作业(?:原图|图片)随提醒发送/);
      assert.equal(await page.locator('#bubble figcaption').count(), 0);
      const sequence = await page.locator('#bubble').evaluate(element => {
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
        let result = '';
        while (walker.nextNode()) {
          if (walker.currentNode.nodeType === Node.TEXT_NODE) result += walker.currentNode.textContent;
          if (walker.currentNode.nodeName === 'IMG') result += '[image]';
        }
        return result;
      });
      assert(sequence.indexOf('[image]') < sequence.indexOf('下一项练习'));
      assert(sequence.lastIndexOf('[image]') < sequence.indexOf('—— 共 3 项'));
      assert(sequence.startsWith('📚 作业提醒'));
      assert.equal(await page.locator('#bubble img').count(), 2);
      await page.screenshot({ path: path.join(output, 'message-baseline.png') });
    } finally {
      await page.locator('#pvClose').click();
    }
  });
  await check('nested schedule editor close button is reachable at desktop height', async () => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.locator('#menuBtn').click();
    await page.locator('#openSystem').click();
    await page.locator('#btnScheduleSet').click();
    await page.waitForSelector('#maskSchedule.on');
    await page.screenshot({ path: path.join(output, 'schedule.png') });
    try {
      await page.locator('#scheduleClose').click({ timeout: 2000 });
      assert.equal(await page.locator('#maskSchedule').evaluate(element => element.classList.contains('on')), false);
    } finally {
      await page.locator('#dlgSystem').evaluate(element => element.close());
    }
  });
  assert.deepEqual(externalRequests, []);
  assert.deepEqual(pageErrors, []);
  assert(taskOperations.every(operation => operation === 'preview'));
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({
    checks, externalRequests, pageErrors, taskOperations,
  }, null, 2));
  console.log(JSON.stringify({ passed: checks.length - failures.length, failed: failures, checks }, null, 2));
  if (failures.length) process.exitCode = 1;
}

main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  clearTimeout(timeout);
  await browser?.close();
  fixture?.kill();
});
