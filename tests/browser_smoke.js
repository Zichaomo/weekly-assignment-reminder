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
    await row.locator('[data-act="content"]').click();
    await page.waitForSelector('.content-detail', { timeout: 5000 });
    assert((await page.locator('.content-detail').innerText()).includes('作图并说明'));

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
      const expectedPollLoss = entry.url === taskGetUrl
        && /net::ERR_FAILED|Failed to load resource/.test(entry.text);
      return !(expectedConflict || expectedPollLoss);
    });
    assert.deepStrictEqual(unexpectedConsole, [],
      'unexpected console errors: ' + JSON.stringify(unexpectedConsole));
    assert(events.consoleErrors.some(entry => entry.url === viewUrl && /status of 409/.test(entry.text)),
      'expected 409 console entry was not observed');

    console.log(JSON.stringify({
      ok: true,
      screenshots: [cropShot, shot],
      persisted_regions: persisted.regions.length,
      cold_open_task_posts: 0,
      task_posts_during_poll_test: events.taskPosts.length,
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
