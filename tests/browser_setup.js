// First deployment and credential editors against the isolated local backend.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { PYTHON, chromium } = require('./browser_runtime');
let fixture, browser;

async function main() {
  const output = path.join(__dirname, 'artifacts', 'setup');
  fs.mkdirSync(output, { recursive: true });
  fixture = spawn(PYTHON, ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    cwd: path.resolve(__dirname, '..'), windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  const info = await new Promise((resolve, reject) => {
    let stdout = '', stderr = '';
    const timer = setTimeout(() => reject(Error('Fixture timeout')), 15000);
    fixture.stderr.on('data', data => { stderr += data; });
    fixture.stdout.on('data', data => {
      stdout += data;
      const line = stdout.split(/\r?\n/).find(value => value.startsWith('{'));
      if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
    });
    fixture.on('exit', () => { clearTimeout(timer); reject(Error(stderr)); });
  });
  fs.unlinkSync(path.join(info.dir, 'config.json'));
  const origin = 'http://127.0.0.1:' + info.port;
  const external = [], errors = [];
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin !== origin) {
      external.push(true); return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin);
  await page.waitForSelector('#dlgSetup[open]');
  await page.screenshot({ animations: 'disabled', path: path.join(output, 'guide.png') });
  assert((await page.locator('#dlgSetup').innerText()).includes('首次部署'));
  await page.locator('#setupAccount').click();
  await page.locator('#accountUsername').fill('fixture-student');
  await page.locator('#accountPassword').fill('fixture-password');
  await page.locator('#accountSave').click();
  await page.waitForFunction(() => document.querySelector('#accountStatus').textContent.includes('已保存'));
  assert.equal(await page.locator('#accountPassword').inputValue(), '');
  const saved = await (await page.request.get(origin + '/api/settings/account')).json();
  assert.equal(saved.username, 'fixture-student');
  assert(!JSON.stringify(saved).includes('fixture-password'));
  await page.screenshot({ animations: 'disabled', path: path.join(output, 'account.png') });
  await page.route('**/api/tasks', route => {
    assert.deepEqual(route.request().postDataJSON(), { op: 'login' });
    return route.fulfill({ json: { ok: true, task_id: 'setup-login' } });
  });
  await page.route('**/api/tasks/setup-login', route => route.fulfill({ json: {
    ok: true, status: 'done', result: { ok: true, login_verified: true },
  } }));
  await page.locator('#accountLogin').click();
  await page.waitForFunction(() => document.querySelector('#accountStatus').textContent.includes('已验证'));
  await page.locator('#accountClose').click();
  await page.locator('#menuBtn').click(); await page.locator('#openWecom').click();
  await page.locator('#wecomChannel').selectOption('wecom');
  await page.locator('#wecomMain').fill('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixture-only');
  await page.locator('#wecomSave').click();
  await page.waitForFunction(() => document.querySelector('#wecomStatus').textContent.includes('已保存'));
  assert.equal(await page.locator('#wecomMain').inputValue(), '');
  assert((await page.locator('#wecomMainState').innerText()).includes('已配置'));
  await page.screenshot({ animations: 'disabled', path: path.join(output, 'wecom.png') });
  await page.route('**/api/settings/wecom', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 409, json: { ok: false, error: '设置版本冲突，请重新打开' } })
    : route.continue());
  await page.locator('#wecomMain').fill('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixture-draft');
  await page.locator('#wecomSave').click();
  await page.waitForFunction(() => document.querySelector('#wecomStatus').textContent.includes('冲突'));
  assert((await page.locator('#wecomMain').inputValue()).includes('fixture-draft'));
  await page.unroute('**/api/settings/wecom');
  const cfg = JSON.parse(fs.readFileSync(path.join(info.dir, 'config.json'), 'utf8'));
  assert.equal(cfg.channel, 'wecom');
  assert(cfg.school_account.protected);
  assert(!JSON.stringify(cfg).includes('fixture-password'));
  await page.locator('#wecomClose').click();
  await page.locator('#menuBtn').click(); await page.locator('#openSetup').click();
  await page.locator('#setupFinish').click();
  await page.reload();
  await page.waitForSelector('#list .item');
  assert.equal(await page.locator('#dlgSetup[open]').count(), 0);
  for (const viewport of [{ width: 900, height: 640 }, { width: 390, height: 700 }]) {
    await page.setViewportSize(viewport);
    await page.locator('#menuBtn').click(); await page.locator('#openAccount').click();
    const box = await page.locator('#dlgAccount').boundingBox();
    assert(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1);
    assert(box.y + box.height <= viewport.height + 1);
    if (viewport.width === 390) await page.screenshot({ animations: 'disabled', path: path.join(output, 'account-small.png') });
    await page.locator('#accountClose').click();
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  console.log('First deployment and settings browser acceptance passed. External requests 0.');
}
main().catch(error => { console.error(error.message); process.exitCode = 1; })
  .finally(async () => { await browser?.close(); fixture?.kill(); });
