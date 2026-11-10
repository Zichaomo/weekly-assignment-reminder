// Functional menu groups, live entry points and viewport visibility.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { PYTHON, chromium } = require('./browser_runtime');
const output = path.join(__dirname, 'artifacts', 'management-menu');
const groups = [
  { id: 'account', title: '账号与推送', entries: ['openAccount', 'openWecom', 'openSystem'] },
  { id: 'content', title: '课程与内容', entries: ['openCourses', 'btnCourseRules', 'exportViews'] },
  { id: 'system', title: '系统与维护', entries: ['openBackend', 'openLogs', 'shutdown'] },
];
let fixture, browser;

async function main() {
  fs.mkdirSync(output, { recursive: true });
  fixture = spawn(PYTHON, ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    cwd: path.resolve(__dirname, '..'), windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  const info = await new Promise((resolve, reject) => {
    let stdout = '', stderr = '';
    const timer = setTimeout(() => reject(Error('Fixture startup timed out')), 15000);
    fixture.stderr.on('data', data => { stderr += data; });
    fixture.stdout.on('data', data => {
      stdout += data;
      const line = stdout.split(/\r?\n/).find(value => value.startsWith('{'));
      if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
    });
    fixture.on('exit', () => { clearTimeout(timer); reject(Error(stderr || 'Fixture stopped')); });
  });
  const origin = 'http://127.0.0.1:' + info.port;
  const external = [], errors = [], requests = [];
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'zh-CN' });
  await context.route('**/*', route => {
    const request = route.request();
    if (new URL(request.url()).origin !== origin) {
      external.push(true); return route.abort();
    }
    requests.push({ path: new URL(request.url()).pathname, method: request.method() });
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin);
  await page.waitForSelector('#list .item');
  async function openMenu() {
    if (!(await page.locator('#menu').evaluate(element => element.classList.contains('open')))) {
      await page.locator('#menuBtn').click();
    }
    await page.waitForFunction(() => getComputedStyle(document.querySelector('#menu')).opacity === '1');
  }
  await openMenu();
  await page.screenshot({ animations: 'disabled', path: path.join(output, 'initial.png') });
  assert.equal(await page.locator('#menu [data-menu-group]').count(), 3, 'three functional menu groups');
  for (const group of groups) {
    const element = page.locator(`[data-menu-group="${group.id}"]`);
    assert.equal(await element.getAttribute('role'), 'group');
    assert.equal(await element.locator('.menu-group-title').innerText(), group.title);
    const ids = await element.locator('button:not([hidden])').evaluateAll(buttons => buttons.map(button => button.id));
    assert.deepEqual(ids, group.entries);
  }
  assert.equal(await page.locator('#openSetup').evaluate(element => element.closest('[data-menu-group]')), null);
  assert.equal(await page.locator('#menu button:not([hidden])').count(), 10);
  assert(await page.locator('#openSetup').evaluate(button => {
    const expected = document.createElement('div');
    expected.style.backgroundColor = getComputedStyle(document.documentElement).getPropertyValue('--olive-wash');
    return getComputedStyle(button).backgroundColor === expected.style.backgroundColor;
  }), 'deployment guide uses the accepted fixed background');
  const guideIcon = await page.locator('#openSetup svg').boundingBox();
  const accountIcon = await page.locator('#openAccount svg').boundingBox();
  assert(Math.abs(guideIcon.x - accountIcon.x) < 1, 'deployment guide stays left aligned');
  assert.notEqual(await page.locator('#exportViews svg path').getAttribute('d'),
    'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0', 'backup entry has a matching local icon');
  const boxes = await Promise.all(groups.map(group => page.locator(`[data-menu-group="${group.id}"]`).boundingBox()));
  assert(boxes[0].x < boxes[1].x && boxes[1].x < boxes[2].x, 'desktop columns sit side by side');
  assert(Math.abs(boxes[0].y - boxes[2].y) < 2, 'desktop columns share their top edge');
  await page.screenshot({ animations: 'disabled', path: path.join(output, 'desktop.png') });
  await page.locator('#menu').screenshot({ animations: 'disabled', path: path.join(output, 'menu-desktop.png') });

  for (const [button, panel, close] of [
    ['openSetup', '#dlgSetup[open]', '#setupClose'],
    ['openAccount', '#dlgAccount[open]', '#accountClose'],
    ['openWecom', '#dlgWecom[open]', '#wecomClose'],
    ['openSystem', '#dlgSystem[open]', '#systemClose'],
    ['openCourses', '#dlgCourses[open]', '#dlgCourses .dlg-foot [data-close]'],
    ['btnCourseRules', '#ruleMask.on', '#ruleClose'],
    ['openBackend', '#dlgBackend[open]', '#backendClose'],
    ['openLogs', '#dlgLogs[open]', '#logsClose'],
  ]) {
    await openMenu();
    await page.locator('#' + button).click();
    await page.waitForSelector(panel);
    await page.locator(close).click();
  }
  await openMenu();
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#exportViews').click();
  const download = await downloadPromise;
  assert.equal(download.suggestedFilename(), 'homework-views.json');
  await download.cancel();
  await openMenu();
  await page.locator('#shutdown').click();
  await page.waitForSelector('#dlgConfirm[open]');
  await page.locator('#dlgConfirm [data-close]').click();
  assert(!requests.some(request => request.path === '/api/shutdown'));

  for (const viewport of [
    { width: 900, height: 640 }, { width: 640, height: 600 },
    { width: 390, height: 700 }, { width: 320, height: 568 }, { width: 640, height: 360 },
  ]) {
    await page.setViewportSize(viewport);
    await openMenu();
    const box = await page.locator('#menu').boundingBox();
    assert(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1);
    assert(box.y + box.height <= viewport.height + 1);
    assert(await page.locator('#menu').evaluate(element => element.scrollWidth <= element.clientWidth + 1));
    for (const id of ['openSetup', ...groups.flatMap(group => group.entries)]) {
      const button = page.locator('#' + id);
      await button.scrollIntoViewIfNeeded();
      const bounds = await button.boundingBox();
      assert(bounds.x >= 0 && bounds.y >= box.y && bounds.x + bounds.width <= viewport.width + 1);
      assert(bounds.y + bounds.height <= box.y + box.height + 1, id + ' remains reachable');
      assert(await button.evaluate(element => element.scrollWidth <= element.clientWidth + 1), id + ' text fits');
    }
    await page.locator('#menu').evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ animations: 'disabled', path: path.join(output, `menu-${viewport.width}-${viewport.height}.png`) });
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#menuBtn').getAttribute('aria-expanded'), 'false');
  }
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
  console.log('Management menu acceptance passed. All 10 entries and six viewport sizes. External requests 0.');
}
main().catch(error => { console.error(error.message); process.exitCode = 1; })
  .finally(async () => { await browser?.close(); fixture?.kill(); });
