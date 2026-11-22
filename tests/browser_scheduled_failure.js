const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { PYTHON, chromium } = require('./browser_runtime');

let fixture, browser, timeout;
async function main() {
  const output = path.join(__dirname, 'artifacts', 'scheduled-failure');
  fs.mkdirSync(output, { recursive: true });
  timeout = setTimeout(() => { fixture?.kill(); browser?.close(); process.exitCode = 1; }, 60000);
  fixture = spawn(PYTHON, ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    cwd: path.resolve(__dirname, '..'), windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  const info = await new Promise((resolve, reject) => {
    let stdout = '';
    const timer = setTimeout(() => reject(Error('fixture timeout')), 12000);
    fixture.stdout.on('data', chunk => {
      stdout += chunk;
      const line = stdout.split(/\r?\n/).find(line => line.startsWith('{'));
      if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
    });
    fixture.once('exit', code => { clearTimeout(timer); reject(Error('fixture exited '+code)); });
  });
  const origin = 'http://127.0.0.1:'+info.port;
  const externalRequests = [], taskPosts = [], errors = [];
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.route('**/*', route => {
    const request = route.request();
    if (new URL(request.url()).origin !== origin) { externalRequests.push(request.url()); return route.abort(); }
    if (request.method() === 'POST' && request.url() === origin+'/api/tasks') taskPosts.push(request.postData());
    return route.continue();
  });
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(6000);
  const today = new Intl.DateTimeFormat('sv-SE').format(new Date());
  const failed = { kind: 'scheduled', time: today+' 11:01:13', channel: 'wecom', ok: false,
    exit_code: 1, error: '自动登录未成功，需人工双击 login.bat 重新登录。\n已发送登录失效告警。' };
  const ledger = path.join(info.dir, 'task_log.jsonl');
  const save = records => fs.writeFileSync(ledger, records.map(record => JSON.stringify(record)).join('\n'));
  save([failed]);
  await page.goto(origin);
  const alert = page.getByRole('dialog', { name: '定时推送失败' });
  await alert.waitFor();
  const body = await alert.innerText();
  assert(body.includes('11:01:13') && body.includes('北化在线') && body.includes('自动续登失败'));
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 700 }, { width: 640, height: 360 }]) {
    await page.setViewportSize(viewport);
    const box = await alert.boundingBox();
    assert(box.x >= 0 && box.y >= 0 && box.x+box.width <= viewport.width+1 && box.y+box.height <= viewport.height+1);
    assert(await alert.getByRole('button', { name: '知道了' }).isVisible());
    await page.screenshot({ animations: 'disabled', path: path.join(output, 'failure-'+viewport.width+'.png') });
  }
  await alert.getByRole('button', { name: '知道了' }).click();
  await alert.waitFor({ state: 'hidden' });
  await page.reload();
  await alert.waitFor();
  await alert.getByRole('button', { name: '查看定时状态' }).click();
  await page.locator('#dlgSystem[open] #scheduleDetails[open]').waitFor();
  save([failed, { ...failed, time: today+' 11:30:42', ok: true, exit_code: 0 }]);
  await page.reload();
  await page.waitForSelector('#list .item');
  assert(!(await alert.isVisible()));
  assert.deepEqual(errors, []);
  assert.deepEqual(externalRequests, []);
  assert.deepEqual(taskPosts, []);
  console.log('Scheduled failure shown at startup, with cause, and suppressed after a successful retry.');
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  clearTimeout(timeout);
  await browser?.close();
  fixture?.kill();
});
