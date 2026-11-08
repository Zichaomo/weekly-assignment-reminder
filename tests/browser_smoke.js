// Offline Edge smoke test: real local HTTP API + isolated SQLite fixture.
// Run: node tests/browser_smoke.js
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..');
const PLAYWRIGHT = process.env.PLAYWRIGHT_PATH ||
  'C:/Users/MECHREVO/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.js';
const PYTHON = process.env.PYTHON || 'D:/Miniconda3/python.exe';
const { chromium } = require(PLAYWRIGHT);

function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function waitUntil(fn, timeout, message) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return;
    await wait(50);
  }
  throw new Error(typeof message === 'function' ? message() : (message || 'condition timeout'));
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  const exited = new Promise(resolve => child.once('exit', resolve));
  child.kill();
  await Promise.race([exited, wait(3000)]);
  if (child.exitCode === null) {
    child.kill('SIGKILL');
    await Promise.race([exited, wait(1000)]);
  }
}

async function cleanupFixtureDir(dir) {
  if (!dir) return;
  const tmpRoot = path.resolve(os.tmpdir());
  const target = path.resolve(dir);
  const insideTmp = target.startsWith(tmpRoot + path.sep);
  const safeName = path.basename(target).startsWith('homework-browser-smoke-');
  if (!insideTmp || !safeName) return;
  for (let i = 0; i < 4; i += 1) {
    try {
      fs.rmSync(target, { recursive: true, force: true });
      return;
    } catch (err) {
      if (i === 3) return;
      await wait(200);
    }
  }
}

async function main() {
  let child;
  let browser;
  let info;
  const events = {
    pageErrors: [], consoleErrors: [], externalRequests: [], requests: [],
    taskPosts: [], dialogs: [], faviconStatuses: [],
  };
  try {
    child = spawn(PYTHON, ['-B', path.join(ROOT, 'tests', 'browser_fixture_server.py')], {
      cwd: ROOT,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
    });
    let stderr = '';
    child.stderr.on('data', data => { stderr += data.toString(); });
    const ready = new Promise((resolve, reject) => {
      let buf = '';
      const timer = setTimeout(() => reject(new Error('fixture timeout: ' + stderr)), 20000);
      child.stdout.on('data', data => {
        buf += data.toString();
        const line = buf.split(/\r?\n/).find(item => item.trim().startsWith('{'));
        if (line) {
          clearTimeout(timer);
          resolve(JSON.parse(line));
        }
      });
      child.on('exit', code => reject(new Error('fixture exited ' + code + ': ' + stderr)));
    });

    info = await ready;
    const base = `http://127.0.0.1:${info.port}`;
    const fixtureOrigin = new URL(base).origin;
    const fixturePort = String(info.port);
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

    // Only the exact fixture origin is routable. Any external URL is aborted,
    // recorded, and treated as a test failure at the end.
    await page.route('**/*', async route => {
      const raw = route.request().url();
      let parsed;
      try { parsed = new URL(raw); } catch (err) { parsed = null; }
      if (!parsed || parsed.origin !== fixtureOrigin || parsed.hostname !== '127.0.0.1'
          || parsed.port !== fixturePort) {
        events.externalRequests.push(raw);
        await route.abort('blockedbyclient');
        return;
      }
      await route.continue();
    });
    page.on('pageerror', err => events.pageErrors.push(String(err)));
    page.on('console', msg => {
      if (msg.type() === 'error') {
        events.consoleErrors.push({ text: msg.text(), url: (msg.location() || {}).url || '' });
      }
    });
    page.on('request', request => {
      events.requests.push(request.url());
      if (request.url().endsWith('/api/tasks') && request.method() === 'POST') {
        events.taskPosts.push(request.postData());
      }
    });
    page.on('dialog', async dialog => {
      try { await dialog.accept(); } catch (err) {}
      events.dialogs.push({ type: dialog.type(), message: dialog.message() });
    });

    // Explicitly exercise the fixture's favicon route rather than suppressing 404s.
    const favicon = await page.request.get(base + '/favicon.ico');
    events.faviconStatuses.push(favicon.status());
    assert.strictEqual(favicon.status(), 204, 'fixture favicon must be handled explicitly');

    const config = await page.request.get(base + '/api/config');
    assert.strictEqual(config.status(), 200);
    assert.strictEqual((await config.json()).channel, 'none', 'fixture channel must be none');

    await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#tbody tr[data-key]', { timeout: 10000 });
    assert.strictEqual(events.taskPosts.length, 0, 'cold open must not start a task');
    assert.strictEqual(await page.locator('#tbody > tr > td.empty').count(), 0,
      'loaded rows must remove the empty placeholder');
    const initialRows = await page.locator('#tbody tr[data-key]').count();
    await page.waitForFunction(() => document.querySelector('#runtimeText').textContent.includes('本地最新版本'));
    assert(!(await page.locator('#scheduleDetails').evaluate(el => el.open)), 'schedule details start folded');
    assert(await page.evaluate(() => !document.querySelector('#scheduleDetails').closest('section').contains(document.querySelector('#runtimeText'))));
    assert(await page.evaluate(() => !!(document.querySelector('#backendSection').compareDocumentPosition(document.querySelector('.foot')) & Node.DOCUMENT_POSITION_FOLLOWING)));
    await page.locator('#scheduleDetails summary').click();
    assert((await page.locator('#scheduleText').innerText()).includes('2099-01-04 09:00:00'));
    await page.locator('#scheduleDetails summary').click();
    let scheduleRevision=1, scheduleActions=[], deniedCancel=false;
    let scheduleTasks=[{name:'BUCT-Homework-Weekly',registered:true,enabled:true,state:3,matches_project:true,schedule_time:'09:00',days:7,next_run:'2099-01-04 09:00:00'},
      {name:'BUCT-Homework-Catchup',registered:true,enabled:true,state:3,matches_project:true,next_run:'2099-01-04 00:05:00'}];
    await page.route(/\/api\/schedule(?:\?.*)?$/, async route => {
      if (route.request().method()==='POST') {
        const p=route.request().postDataJSON();scheduleActions.push(p);
        assert.strictEqual(p.expected_revision,String(scheduleRevision));
        if(p.action==='cancel' && !deniedCancel) {
          deniedCancel=true;
          await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({ok:false,error:'未完成Windows权限确认，定时设置未保存。'})});return;
        }
        if(p.action==='set') {scheduleTasks[0].enabled=true;scheduleTasks[0].schedule_time=p.time;scheduleTasks[0].days=p.days;scheduleTasks[1].enabled=p.catchup;}
        else scheduleTasks.forEach(t=>t.enabled=false);
        scheduleRevision++;
        await route.fulfill({status:200,contentType:'application/json',body:'{"ok":true,"note":"定时设置已更新"}'});return;
      }
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,available:true,tasks:scheduleTasks,revision:String(scheduleRevision),last_attempt:{},last_success:{}})});
    });
    scheduleTasks[0].next_run='2099-01-04 09:30:00';
    await page.locator('#btnSystemRefresh').click();
    await page.waitForFunction(() => document.querySelector('#scheduleSummary').textContent.includes('下次补推检查'));
    assert(!(await page.locator('#scheduleSummary').innerText()).includes('下次首次发布'));
    assert((await page.locator('#scheduleText').textContent()).includes('后续触发均为补推检查'));
    assert((await page.locator('#scheduleText').textContent()).includes('已成功则跳过'));
    assert((await page.locator('#scheduleText').textContent()).includes('旧版额外补推任务'));
    scheduleTasks[0].next_run='2099-01-04 09:00:00';
    await page.locator('#btnSystemRefresh').click();
    await page.waitForFunction(() => document.querySelector('#scheduleSummary').textContent.includes('首次发布检查'));
    await page.locator('#btnScheduleSet').click();
    await page.locator('#maskSchedule.on').waitFor();
    await page.locator('#scheduleTime').fill('10:30');await page.locator('#scheduleDays').fill('14');
    await page.locator('#scheduleSave').click();
    await page.waitForFunction(() => !document.querySelector('#maskSchedule').classList.contains('on'));
    await page.waitForFunction(() => !document.querySelector('#btnScheduleCancel').disabled);
    await page.locator('#btnScheduleCancel').click();
    await page.waitForFunction(() => document.querySelector('#scheduleActionStatus').textContent.includes('未完成Windows权限确认'));
    assert(await page.locator('#scheduleActionStatus').isVisible());
    await page.waitForFunction(() => !document.querySelector('#btnScheduleCancel').disabled);
    await page.locator('#btnScheduleCancel').click();
    await page.waitForFunction(() => document.querySelector('#scheduleSummary').textContent==='定时发布未开启');
    assert.deepStrictEqual(scheduleActions.map(p=>p.action),['set','cancel','cancel']);
    assert.strictEqual(scheduleActions[0].time,'10:30');assert.strictEqual(scheduleActions[0].days,14);
    assert.strictEqual(scheduleActions[0].catchup,false);
    assert(scheduleTasks.every(t=>!t.enabled));
    await page.locator('#scheduleDetails summary').click();
    const stoppedDetails=await page.locator('#scheduleText').innerText();
    assert(stoppedDetails.includes('已停用，不会自动运行'));
    assert(!stoppedDetails.includes('下次运行：'));
    assert(!stoppedDetails.includes('2099-01-04'));
    await page.locator('#scheduleDetails summary').click();
    await page.locator('#btnScheduleSet').click();
    await page.locator('#maskSchedule.on').waitFor();
    assert.strictEqual(await page.locator('#scheduleCatchup').count(),0, 'settings must not recreate retired midnight task');
    await page.locator('#scheduleClose').click();
    events.dialogs.length=0;
    await page.unroute(/\/api\/schedule(?:\?.*)?$/);
    await page.locator('#btnSystemRefresh').click();
    assert(await page.locator('#btnUpdateBackend').isDisabled());
    let systemPid=100, systemOld=true, systemBusy=true, restartPosts=0;
    await page.route('**/api/runtime', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
      ok:true,pid:systemPid,running_version:systemOld?'old':'new',disk_version:'new',needs_update:systemOld,busy:systemBusy})}));
    await page.locator('#btnSystemRefresh').click();
    await page.waitForFunction(() => document.querySelector('#runtimeHint').textContent.includes('排队'));
    assert(await page.locator('#btnUpdateBackend').isDisabled());
    systemBusy=false;
    await page.locator('#btnSystemRefresh').click();
    await page.waitForFunction(() => !document.querySelector('#btnUpdateBackend').disabled);
    await page.locator('#addT').fill('更新后保留的草稿');
    await page.route('**/api/runtime/update', async route => {
      restartPosts+=1;systemPid=101;systemOld=false;
      await route.fulfill({status:202,contentType:'application/json',body:'{"ok":true}'});
    });
    await page.locator('#btnUpdateBackend').click();
    await page.waitForFunction(() => document.querySelector('#runtimeText').textContent.includes('本地最新版本'));
    assert.strictEqual(await page.locator('#addT').inputValue(),'更新后保留的草稿');
    assert.strictEqual(restartPosts,1);
    await page.locator('#addT').fill('');
    await page.unroute('**/api/runtime');await page.unroute('**/api/runtime/update');
    await page.locator('#btnSystemRefresh').click();
    const systemShot=path.join(ROOT,'tests','artifacts','browser-smoke-system.png');
    fs.mkdirSync(path.dirname(systemShot),{recursive:true});
    await page.screenshot({path:systemShot});
    await page.locator('#btnCourseRules').click();
    await page.waitForSelector('#ruleMask.on');
    assert.strictEqual(await page.locator('#ruleList .rule-row').count(), 4);
    await page.locator('#ruleNew').click();
    await page.locator('#ruleCourse').fill('fixture课程 A');
    await page.locator('#ruleMode').selectOption('full');
    await page.locator('#ruleLines').fill('2');
    await page.locator('#ruleText').fill('任务一\n任务二\n任务三');
    await page.locator('#rulePreview').click();
    await page.waitForFunction(() => document.querySelector('#ruleResult').textContent.includes('任务二'));
    assert(!(await page.locator('#ruleResult').innerText()).includes('任务三'));
    await page.locator('#ruleSave').click();
    await page.waitForFunction(() => document.querySelector('#ruleStatus').textContent.includes('已保存'));
    const ruleShot = path.join(ROOT,'tests','artifacts','browser-smoke-course-rules.png');
    await page.screenshot({path:ruleShot});
    await page.locator('#ruleClose').click();
    await page.locator('#btnCourseRules').click();
    await page.locator('#ruleList .rule-row').filter({hasText:'fixture课程 A'}).click();
    assert.strictEqual(await page.locator('#ruleLines').inputValue(), '2');
    await page.locator('#ruleReset').click();
    await page.waitForFunction(() => document.querySelector('#ruleStatus').textContent.includes('已恢复'));
    await page.locator('#ruleClose').click();
    assert(events.dialogs.some(d => d.type === 'confirm' && d.message.includes('删除这条自定义规则')));
    events.dialogs.length = 0;
    const effectiveRow = page.locator('#tbody tr[data-key]').filter({ hasText: 'fixture有效摘要' });
    // Automatic fill must stay in the visible date window and attempt failures once.
    await page.evaluate(() => {
      window.__originalRunTask = runTask;
      window.__autoRequests = [];
      const deadline = offset => new Date(Date.now()+offset*86400000).toISOString().slice(0,16).replace('T',' ');
      state.platformRows = Array.from({length:14},(_,i)=>({item_key:'window-'+i,kind:'homework',deadline:deadline(2),content_ready:false}));
      state.platformRows.push({item_key:'history',deadline:deadline(-10)}, {item_key:'distant',deadline:deadline(40)});
      runTask = (op,params,label,done) => {
        window.__autoRequests.push(params.item_keys);
        setTimeout(()=>done({ok:false,failed:params.item_keys.length,remaining:99,items:[]}),0);
      };
      autoFillVisibleContent();
    });
    await page.waitForFunction(() => window.__autoRequests.length===2 && !state.autoFilling);
    const attempted = await page.evaluate(() => { runTask=window.__originalRunTask; return window.__autoRequests.flat(); });
    assert.strictEqual(attempted.length,14);
    assert.strictEqual(new Set(attempted).size,14);
    assert(attempted.every(k=>k.startsWith('window-')));
    await page.evaluate(()=>refreshList({silent:true}));
    assert((await effectiveRow.locator('.effective-summary').innerText()).includes('习题：10-11、10-13、10-21。'));
    assert(!(await effectiveRow.locator('.content-brief').innerText()).includes('提交图片'));
    assert.strictEqual(await effectiveRow.locator('.content-detail').count(), 0, 'full body starts folded');
    await effectiveRow.locator('[data-act="content"]').click();
    assert((await effectiveRow.locator('.content-detail').innerText()).includes('提交图片'), 'original boilerplate remains accessible');
    await effectiveRow.locator('[data-act="content"]').click();
    const genericRow = page.locator('#tbody tr[data-key]').filter({ hasText: 'fixture通用说明' });
    assert((await genericRow.locator('.effective-summary').innerText()).includes('没有具体题目'));
    assert(!(await genericRow.locator('.content-brief').innerText()).includes('正放，不要倒置'));
    const summaryShot = path.join(ROOT, 'tests', 'artifacts', 'browser-smoke-summary.png');
    fs.mkdirSync(path.dirname(summaryShot), { recursive: true });
    await effectiveRow.screenshot({ path: summaryShot });

    // Save an explicit publishing decision through the real endpoint. A conflict
    // must keep the editor/draft, and reload must retain successful decisions.
    const effectiveKey = await effectiveRow.getAttribute('data-key');
    const reviewUrl = base + '/api/assignments/' + encodeURIComponent(effectiveKey) + '/review';
    let reviewConflict = true;
    let oldReviewBackend = true;
    const conflictReviewRoute = async route => {
      if (oldReviewBackend) {
        oldReviewBackend = false;
        await route.fulfill({ status: 404, contentType: 'application/json',
          body: JSON.stringify({ ok: false, error: 'not found' }) });
      } else if (reviewConflict) {
        reviewConflict = false;
        await route.fulfill({ status: 409, contentType: 'application/json',
          body: JSON.stringify({ ok: false, code: 'version_conflict', error: '正文版本已更新' }) });
      } else await route.fallback();
    };
    await page.route(reviewUrl, conflictReviewRoute);
    await effectiveRow.getByText('确认推送内容', { exact: true }).click();
    await page.locator('#reviewText').fill('10-11、10-13');
    const reviewShot = path.join(ROOT, 'tests', 'artifacts', 'browser-smoke-review.png');
    await page.screenshot({ path: reviewShot });
    await page.locator('#reviewSave').click();
    await page.waitForFunction(() => document.querySelector('#reviewStatus').textContent.includes('后台仍在使用旧版本'));
    assert.strictEqual(await page.locator('#reviewText').inputValue(), '10-11、10-13');
    await page.locator('#reviewSave').click();
    await page.waitForFunction(() => document.querySelector('#reviewStatus').textContent.includes('正文版本已更新'));
    assert(await page.locator('#maskReview').evaluate(el => el.classList.contains('on')));
    assert.strictEqual(await page.locator('#reviewText').inputValue(), '10-11、10-13');
    await page.unroute(reviewUrl, conflictReviewRoute);
    await page.locator('#reviewSave').click();
    await page.waitForFunction(() => !document.querySelector('#maskReview').classList.contains('on'));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await effectiveRow.getByText('修改确认结果', { exact: true }).waitFor();
    assert((await effectiveRow.locator('.effective-summary').innerText()).includes('已人工确认 · 推送此正文'));
    assert((await effectiveRow.locator('.effective-summary').innerText()).includes('10-11、10-13'));
    assert(!(await effectiveRow.locator('.effective-summary').innerText()).includes('10-21'));
    await effectiveRow.getByText('修改确认结果', { exact: true }).click();
    await page.locator('#reviewOmit').click();
    await page.waitForFunction(() => !document.querySelector('#maskReview').classList.contains('on'));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await effectiveRow.getByText('修改确认结果', { exact: true }).waitFor();
    assert((await effectiveRow.locator('.effective-summary').innerText()).includes('已人工确认 · 不推送正文'));
    assert((await effectiveRow.locator('.title-line').innerText()).includes('fixture有效摘要'));
    const invalidReview = await page.request.post(reviewUrl, {data: {
      expected_version: 'old-version', mode: 'include', text: '不会保存的旧版本',
    }});
    assert.strictEqual(invalidReview.status(), 409);
    await effectiveRow.getByText('修改确认结果', { exact: true }).click();
    await page.locator('#reviewAuto').click();
    await page.waitForFunction(() => !document.querySelector('#maskReview').classList.contains('on'));
    await effectiveRow.getByText('确认推送内容', { exact: true }).waitFor();
    assert((await effectiveRow.locator('.effective-summary').innerText()).includes('10-21'));
    assert.strictEqual(events.taskPosts.length, 0, 'confirmation must not start scans, body fetches or sends');

    // Date/window changes are local reads only.
    await page.evaluate(() => {
      const days = document.querySelector('#numDays');
      days.value = '14';
      days.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await wait(700);
    assert.strictEqual(events.taskPosts.length, 0, 'date filter must not start platform/content tasks');

    const row = page.locator('#tbody tr[data-key]').first();
    const rowKey = await row.getAttribute('data-key');
    const previewAsset = await page.evaluate(async key => {
      const content = await (await fetch('/api/assignments/' + encodeURIComponent(key) + '/content')).json();
      const entry = {item_key:key, asset_id: content.asset_ids[0], course: '复变函数', title: '<script>preview</script>'};
      const message='• 复变函数｜<script>preview</script>\n   作业原图随提醒发送（1 张）。\n• 下一门课｜普通作业';
      showPreview('原图预览验证', {message, message_personal:message, images: [entry], images_personal: [entry]});
      return entry.asset_id;
    }, rowKey);
    await page.waitForFunction(() => [...document.querySelectorAll('#pane-preview img')].every(im => im.complete && im.naturalWidth > 0));
    assert.strictEqual(await page.locator('#pane-preview img').count(), 2);
    assert.strictEqual(await page.locator('#pane-preview script').count(), 0);
    assert(await page.locator('#pane-preview').evaluate(pane => {
      const nodes=[...pane.childNodes];
      const i=nodes.findIndex(n => n.nodeName==='IMG');
      return nodes[i-1].textContent.includes('复变函数') && nodes[i+1].textContent.includes('下一门课');
    }), 'preview must interleave original image and assignment text');
    assert((await page.locator('#pane-preview img').first().getAttribute('src')).includes(previewAsset));
    const imagePreviewShot = path.join(ROOT, 'tests', 'artifacts', 'browser-smoke-image-preview.png');
    await page.locator('#pane-preview').screenshot({path: imagePreviewShot});
    await row.locator('[data-act="content"]').click();
    await page.waitForSelector('.content-detail', { timeout: 5000 });
    assert((await page.locator('.content-detail').innerText()).includes('作图并说明'));

    // Exercise the actual attachment control without opening an external app.
    let attachmentOpened = null;
    await page.route('**/api/open', async route => {
      attachmentOpened = route.request().postDataJSON().url;
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true }) });
    });
    await page.locator('.content-detail .attach').getByText('打开附件').click();
    await waitUntil(() => attachmentOpened !== null, 5000);
    assert.strictEqual(attachmentOpened, 'https://course.buct.edu.cn/fixture-assignment.pdf');

    // A purely graphical assignment must advertise images even with no text.
    const pureImage = await page.evaluate(() => {
      const host = document.createElement('div');
      const r = state.allRows.find(x => x.item_key);
      const image = collectContentImages(r.content_blocks).find(x => x.status === 'ready');
      renderContentBrief({ ...r, content_text: '', content_blocks: [
        { type: 'paragraph', segments: [image] }], view: null }, host);
      return { label: host.querySelector('.pure-image')?.textContent,
        imageCount: host.querySelectorAll('img.thumb').length,
        empty: host.textContent.includes('平台未填写') };
    });
    assert.strictEqual(pureImage.label, '图片作业 · 1 张图');
    assert.strictEqual(pureImage.imageCount, 1);
    assert.strictEqual(pureImage.empty, false);

    const cellImage = page.locator('.content-detail td img[alt="表格单元格图片"]');
    await cellImage.waitFor({ state: 'visible', timeout: 5000 });
    await cellImage.scrollIntoViewIfNeeded();
    await page.waitForFunction(el => el.complete && el.naturalWidth >= 400 && el.naturalHeight >= 300,
      await cellImage.elementHandle(), { timeout: 5000 });
    assert.strictEqual(await cellImage.evaluate(el => el.naturalWidth), 480);
    assert.strictEqual(await cellImage.evaluate(el => el.naturalHeight), 360);

    const img = page.locator('.content-detail img[alt="正文图片"]');
    await img.waitFor({ state: 'visible', timeout: 5000 });
    await img.scrollIntoViewIfNeeded();
    await page.waitForFunction(el => el.complete && el.naturalWidth >= 400 && el.naturalHeight >= 300,
      await img.elementHandle(), { timeout: 5000 });

    async function drag(canvas, x1, y1, x2, y2) {
      const box = await canvas.boundingBox();
      await page.mouse.move(box.x + box.width * x1, box.y + box.height * y1);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width * x2, box.y + box.height * y2, { steps: 5 });
      await page.mouse.up();
    }

    await img.click();
    await page.waitForSelector('#maskCrop.on', { timeout: 5000 });
    await page.waitForFunction(() => document.querySelector('#cropCanvas').width > 0);
    const canvas = page.locator('#cropCanvas');
    await drag(canvas, 0.08, 0.10, 0.45, 0.45);
    await drag(canvas, 0.52, 0.55, 0.92, 0.92);
    let regionText = await page.locator('#cropRegions').innerText();
    assert(regionText.includes('#1') && regionText.includes('#2'), 'two regions required');
    await page.locator('#cropUndo').click();
    regionText = await page.locator('#cropRegions').innerText();
    assert(regionText.includes('#1') && !regionText.includes('#2'), 'undo failed');
    await page.locator('#cropRedo').click();
    regionText = await page.locator('#cropRegions').innerText();
    assert(regionText.includes('#2'), 'redo failed');
    await page.locator('#cropSave').click();
    await page.waitForFunction(() => !document.querySelector('#maskCrop').classList.contains('on'),
      null, { timeout: 8000 });

    // Reload from the real isolated API and confirm persisted regions.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#tbody tr[data-key]', { timeout: 10000 });
    await page.locator('#tbody tr[data-key]').first().locator('[data-act="content"]').click();
    // Inline images must show saved regions in the detail, not only in the editor.
    await page.waitForFunction(() =>
      document.querySelectorAll('.content-detail p .crop-thumbs canvas').length === 2,
      null, { timeout: 5000 });
    const cropSizes = await page.locator('.content-detail p .crop-thumbs canvas')
      .evaluateAll(canvases => canvases.map(c => [c.width, c.height]));
    assert(cropSizes.every(([w, h]) => w > 20 && h > 20), 'saved crop previews must draw actual pixels');
    const inlineShot = path.join(ROOT, 'tests', 'artifacts', 'browser-smoke-inline-crops.png');
    fs.mkdirSync(path.dirname(inlineShot), { recursive: true });
    await page.locator('.content-detail p .crop-thumbs').screenshot({ path: inlineShot });
    await page.locator('.content-detail img[alt="正文图片"]').first().click();
    await page.waitForSelector('#maskCrop.on', { timeout: 5000 });
    await page.waitForFunction(() => document.querySelectorAll('#cropRegions div').length === 2,
      null, { timeout: 5000 });

    const outDir = path.join(ROOT, 'tests', 'artifacts');
    fs.mkdirSync(outDir, { recursive: true });
    const cropShot = path.join(outDir, 'browser-smoke-crop.png');
    await page.screenshot({ path: cropShot, fullPage: false });

    // Real UI save conflict: the next view POST gets 409. The page must alert
    // before closing, and the server-side saved regions must stay untouched.
    const viewUrl = base + '/api/assignments/' + encodeURIComponent(rowKey) + '/view';
    let viewPosts = 0;
    await page.route(/\/api\/assignments\/[^/]+\/view$/, async route => {
      if (route.request().method() === 'POST') {
        viewPosts += 1;
        await route.fulfill({
          status: 409,
          contentType: 'application/json; charset=utf-8',
          body: JSON.stringify({
            ok: false, code: 'version_conflict',
            error: 'fixture: version changed during save',
          }),
        });
        return;
      }
      await route.continue();
    });
    const modalWasOpenBeforeConflict = await page.locator('#maskCrop')
      .evaluate(el => el.classList.contains('on'));
    assert(modalWasOpenBeforeConflict, 'crop dialog must be open before conflict save');
    await page.locator('#cropSave').click();
    await waitUntil(() => events.dialogs.length === 1, 5000,
      () => `409 alert not shown (viewPosts=${viewPosts})`);
    assert.strictEqual(viewPosts, 1, 'conflict save must POST exactly once');
    assert.strictEqual(events.dialogs[0].type, 'alert');
    assert(events.dialogs[0].message.includes('没有覆盖'),
      'conflict alert must explain that the old crop did not overwrite');
    assert(modalWasOpenBeforeConflict, 'conflict must not silently close the crop dialog');
    await page.waitForFunction(() => !document.querySelector('#maskCrop').classList.contains('on'),
      null, { timeout: 8000 });
    const persisted = await page.evaluate(async key => {
      const response = await fetch('/api/assignments/' + encodeURIComponent(key) + '/content');
      return (await response.json()).view;
    }, rowKey);
    assert.strictEqual(persisted.regions.length, 2, '409 must not overwrite saved regions');
    assert.strictEqual(persisted.source_version, 'fixture-v1');

    // Task polling recovery: first status request loses the connection, the
    // next poll succeeds and clears the connection warning.
    const taskId = 'fixture-reconnect';
    const taskGetUrl = base + '/api/tasks/' + taskId;
    let taskPolls = 0;
    await page.route('**/api/tasks', async route => {
      if (route.request().method() === 'POST') {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ ok: true, task_id: taskId, deduped: false, status: 'queued' }),
        });
        return;
      }
      await route.continue();
    });
    await page.route('**/api/tasks/' + taskId, async route => {
      taskPolls += 1;
      if (taskPolls === 1) {
        await route.abort('failed');
        return;
      }
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true, status: 'done', result: { ok: true }, error: null }),
      });
    });
    await page.evaluate(() => {
      window.__fixtureTaskDone = null;
      runTask('content', { item_keys: ['fixture-noop'], force: false },
        'fixture任务轮询', result => { window.__fixtureTaskDone = result; },
        { inlineOnly: true, globalBusy: false });
    });
    await page.waitForFunction(() => document.querySelector('#connState').textContent.includes('连接中断'),
      null, { timeout: 5000 });
    await page.waitForFunction(() => window.__fixtureTaskDone !== null, null, { timeout: 5000 });
    assert(taskPolls >= 2, 'polling must retry after a lost response');
    const connAfter = await page.locator('#connState').innerText();
    assert(!connAfter.includes('连接中断'), 'connection warning must clear after recovery');

    // Direct guard probe (not page-routed): platform fetch must be refused.
    const guard = await page.request.post(base + '/api/tasks', { data: { op: 'fetch', days: 7 } });
    assert.strictEqual(guard.status(), 500, 'fixture platform task must fail explicitly');
    const guardBody = await guard.json();
    assert(String(guardBody.error || '').includes('FixtureIsolationError'));

    const shot = path.join(outDir, 'browser-smoke.png');
    await page.screenshot({ path: shot, fullPage: false });
    // Failed fetches must not enqueue a second wave of content failures.
    const beforeFailedFetch = events.taskPosts.length;
    await page.route('**/api/tasks', async route => {
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true, task_id: 'fixture-auth-failed', status: 'queued' }) });
    });
    await page.route('**/api/tasks/fixture-auth-failed', async route => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        ok: true, status: 'error', result: { ok: false, error_code: 'auth_required', error: '登录已过期' } }) });
    });
    await page.locator('#btnFetch').click();
    await page.waitForFunction(() => !state.busyTasks.size);
    await wait(700);
    assert.strictEqual(events.taskPosts.length, beforeFailedFetch + 1, 'auth failure must not enqueue content');
    await page.evaluate(() => renderStats({ first_screen_ms: 42, cache: {
      publish_block_reasons: ['10 门课程抓取失败', '认证失败：请重新登录', '缓存超过300秒', '<script>fixture</script>'],
      failed_courses: ['101'], tests_missing_courses: ['101'], last_scan_status: 'auth_failed' } }));
    const banner = page.locator('.status-alert');
    assert((await banner.locator('.status-alert-title').innerText()).includes('登录已过期'));
    assert.notStrictEqual(await banner.evaluate(el => getComputedStyle(el).backgroundColor), 'rgba(0, 0, 0, 0)');
    assert.strictEqual(await banner.locator('script').count(), 0);
    assert.strictEqual(await banner.locator('details').getAttribute('open'), null);
    const alertShot = path.join(outDir, 'browser-smoke-auth-alert.png');
    await page.locator('#stats').screenshot({ path: alertShot });
    await page.setViewportSize({ width: 760, height: 900 });
    const bounds = await banner.boundingBox();
    assert(bounds.x >= 0 && bounds.x + bounds.width <= 761, 'warning must wrap within viewport');
    await page.setViewportSize({ width: 1440, height: 1000 });
    // Daily operations use the same real isolated HTTP handlers as the app.
    const dailyDeadline = new Date(Date.now() + 2 * 86400000);
    const pad = n => String(n).padStart(2, '0');
    const dateText = `${dailyDeadline.getFullYear()}-${pad(dailyDeadline.getMonth()+1)}-${pad(dailyDeadline.getDate())} 23:59`;
    await page.locator('#addC').fill('fixture手动课程');
    await page.locator('#addT').fill('fixture手动新任务');
    await page.locator('#addD').fill(dateText);
    await page.locator('#btnAdd').click();
    await page.waitForFunction(() => state.manualRows.some(r => r.title === 'fixture手动新任务'));
    const manualId = await page.evaluate(() => state.manualRows.find(r => r.title === 'fixture手动新任务').manual_id);
    const edited = await page.request.post(base + '/api/manual', { data: { action: 'edit', id: manualId, title: 'fixture已编辑' } });
    assert.strictEqual((await edited.json()).ok, true);
    await page.evaluate(() => refreshList({ silent: true }));
    assert(await page.getByText('fixture已编辑', { exact: true }).isVisible());
    const removed = await page.request.post(base + '/api/manual', { data: { action: 'rm', id: manualId } });
    assert.strictEqual((await removed.json()).ok, true);
    await page.evaluate(() => refreshList({ silent: true }));
    assert.strictEqual(await page.getByText('fixture已编辑', { exact: true }).count(), 0);
    for (const action of ['apply', 'remove']) {
      const response = await page.request.post(base + '/api/blacklist', { data: { kind: 'course', action, lids: ['101'] } });
      assert.strictEqual((await response.json()).ok, true);
      await page.evaluate(() => refreshList({ silent: true }));
      assert.strictEqual(await page.locator('#tbody tr[data-key]').count(), action === 'apply' ? 0 : initialRows);
    }
    await wait(300);

    assert.deepStrictEqual(events.pageErrors, [], 'page errors: ' + events.pageErrors.join('; '));
    assert.deepStrictEqual(events.externalRequests, [],
      'external requests were attempted: ' + events.externalRequests.join('; '));
    const requestOrigins = new Set(events.requests.map(raw => {
      try { return new URL(raw).origin; } catch (err) { return raw; }
    }));
    assert.deepStrictEqual([...requestOrigins], [fixtureOrigin],
      'page requests must stay on the fixture origin');

    // Deliberately narrow: only the exact 409 save URL and the exact task-poll
    // URL that we made fail may emit console errors.
    const unexpectedConsole = events.consoleErrors.filter(entry => {
      const expectedConflict = entry.url === viewUrl && /status of 409/.test(entry.text);
      const expectedReviewConflict = entry.url === reviewUrl && /status of (409|404)/.test(entry.text);
      const expectedScheduleConflict = entry.url === base + '/api/schedule' && /status of 409/.test(entry.text);
      const expectedPollLoss = entry.url === taskGetUrl
        && /net::ERR_FAILED|Failed to load resource/.test(entry.text);
      return !(expectedConflict || expectedReviewConflict || expectedPollLoss || expectedScheduleConflict);
    });
    assert.deepStrictEqual(unexpectedConsole, [],
      'unexpected console errors: ' + JSON.stringify(unexpectedConsole));
    assert(events.consoleErrors.some(entry => entry.url === viewUrl && /status of 409/.test(entry.text)),
      'expected 409 console entry was not observed');

    console.log(JSON.stringify({
      ok: true,
      screenshots: [cropShot, shot, inlineShot, summaryShot, alertShot, reviewShot, imagePreviewShot, systemShot],
      persisted_regions: persisted.regions.length,
      cold_open_task_posts: 0,
      task_posts_during_poll_test: beforeFailedFetch,
      task_posts_failed_fetch: events.taskPosts.length - beforeFailedFetch,
      task_polls: taskPolls,
      conflict_view_posts: viewPosts,
      console_errors_exact: events.consoleErrors,
      external_requests: events.externalRequests.length,
      favicon_status: events.faviconStatuses,
    }, null, 2));
  } finally {
    if (browser) await browser.close().catch(() => {});
    await stopChild(child);
    await cleanupFixtureDir(info && info.dir);
  }
}

main().catch(err => { console.error(err.stack || String(err)); process.exit(1); });
