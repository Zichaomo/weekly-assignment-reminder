const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { PYTHON, chromium } = require('./browser_runtime');

let child, browser, timer;
const output = path.join(__dirname, 'artifacts', 'task-status');
async function main() {
  fs.mkdirSync(output, { recursive: true });
  timer = setTimeout(() => { child?.kill(); browser?.close(); process.exitCode = 1; }, 60000);
  child = spawn(PYTHON, ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    cwd: path.resolve(__dirname, '..'), windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  let stderr = '';
  child.stderr.on('data', value => { stderr += value; });
  const info = await new Promise((resolve, reject) => {
    let text = '';
    const wait = setTimeout(() => reject(Error('fixture timeout '+stderr)), 10000);
    child.stdout.on('data', value => {
      text += value;
      const line = text.split(/\r?\n/).find(line => line.startsWith('{'));
      if (line) { clearTimeout(wait); resolve(JSON.parse(line)); }
    });
    child.on('exit', () => reject(Error(stderr)));
  });
  const base = 'http://127.0.0.1:'+info.port;
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [], external = [];
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(5000);
  await page.route('**/*', route => {
    if (new URL(route.request().url()).origin !== base) {
      external.push(route.request().url());
      return route.abort();
    }
    return route.continue();
  });
  const renewing = '登录状态已过期，正在自动续登';
  let status = 'running', message = renewing;
  let progress = { done: 0, total: 0, status: 'auth_failed', error: 'authentication failed before list scan' };
  const task = () => ({ ok: true, task_id: 'fixture-login', op: 'fetch', status,
    current_message: message, progress, result: status === 'error' ? { ok: false, error: '自动续登未成功，请重新登录' } : null });
  await page.route('**/api/tasks/fixture-login', route => route.fulfill({ json: task() }));
  await page.route('**/api/runs?*', route => route.fulfill({ json: {
    ok: true, next_cursor: null, history: [], active: [{ ...task(), created_at: Date.now()/1000,
      elapsed_sec: 20, events: [{ at: Date.now()/1000, message }] }],
  } }));
  await page.goto(base);
  await page.waitForSelector('#list .item');
  await page.evaluate(() => { window.statusCheck = pollTask('fixture-login', 'fetch').catch(report); });
  await page.waitForFunction(() => document.querySelector('#taskStatus').textContent.includes('运行中'));
  assert((await page.locator('#taskStatus').innerText()).includes(renewing), 'homepage must show automatic login activity');
  assert(!(await page.locator('#taskStatus').innerText()).includes('已完成'));
  for (const viewport of [{ width: 1440, height: 900 }, { width: 900, height: 640 }, { width: 390, height: 700 }]) {
    await page.setViewportSize(viewport);
    const banner = page.locator('#taskStatus');
    assert(await banner.isVisible());
    const box = await banner.boundingBox();
    assert(box.x >= 0 && box.y >= 0 && box.x+box.width <= viewport.width+1 && box.y+box.height <= viewport.height+1);
    assert(await banner.evaluate(element => element.scrollWidth <= element.clientWidth+1), 'login message must remain readable');
    await page.screenshot({ path: path.join(output, `renewing-${viewport.width}.png`) });
  }
  progress = { ...progress, total: 10 };
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#taskStatus').textContent.includes('正在自动续登'));
  assert(!(await page.locator('#taskStatus').innerText()).includes('已完成'));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator('#menuBtn').click();
  await page.locator('#openLogs').click();
  await page.waitForFunction(() => document.querySelector('#runActive').textContent.includes('正在自动续登'));
  assert.equal(await page.locator('#runActive .run-error').count(), 0);
  assert(!(await page.locator('#runActive').innerText()).includes('已完成 0/10'));
  await page.screenshot({ path: path.join(output, 'renewing-history.png') });
  await page.locator('#logsClose').click();
  message = '自动续登成功，正在重新检索课程';
  progress = { done: 3, total: 10, status: 'running' };
  await page.waitForFunction(() => document.querySelector('#taskStatus').textContent.includes('已完成 3/10'));
  assert(!(await page.locator('#taskStatus').innerText()).includes('正在自动续登'));
  await page.screenshot({ path: path.join(output, 'scanning.png') });
  status = 'error';
  message = renewing;
  await page.waitForFunction(() => document.querySelector('#taskStatus').hidden);
  await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('自动续登未成功'));
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  console.log('Task status acceptance passed. Login renewal, page reload, scan recovery, failure and three viewports.');
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  clearTimeout(timer);
  await browser?.close();
  child?.kill();
});
