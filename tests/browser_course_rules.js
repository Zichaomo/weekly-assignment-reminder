const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { PYTHON, chromium } = require('./browser_runtime');

const output = path.join(__dirname, 'artifacts', 'course-rules');
let child, browser, timer;
async function main() {
  fs.mkdirSync(output, { recursive: true });
  timer = setTimeout(() => { child?.kill(); browser?.close(); process.exitCode = 1; }, 60000);
  child = spawn(PYTHON, ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    cwd: path.resolve(__dirname, '..'), windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', HOMEWORK_RULE_REMINDER_FIXTURE: '1' },
  });
  let stderr = '';
  child.stderr.on('data', data => { stderr += data; });
  const info = await new Promise((resolve, reject) => {
    let data = '';
    const wait = setTimeout(() => reject(Error('fixture timeout ' + stderr)), 10000);
    child.stdout.on('data', value => {
      data += value;
      const line = data.split(/\r?\n/).find(text => text.startsWith('{'));
      if (line) { clearTimeout(wait); resolve(JSON.parse(line)); }
    });
    child.on('exit', () => reject(Error(stderr)));
  });
  const base = 'http://127.0.0.1:' + info.port;
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const external = [], errors = [];
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin !== base) { external.push(true); return route.abort(); }
    return route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  page.setDefaultTimeout(5000);
  await page.goto(base);
  await page.waitForSelector('#list .item');
  const open = async () => {
    await page.locator('#menuBtn').click();
    await page.locator('#btnCourseRules').click();
    await page.waitForSelector('#ruleMask.on');
  };
  await open();
  await page.locator('#rulePending').waitFor({ state: 'visible' });
  assert.match(await page.locator('#rulePending').innerText(), /需要补充推送规则/);
  await page.screenshot({ path: path.join(output, 'reminder.png') });
  await page.locator('[data-rule-needed]').filter({ hasText: 'fixture课程 A' }).click();
  assert.equal(await page.locator('#ruleCourse').inputValue(), 'fixture课程 A');
  assert.equal(await page.locator('#ruleMode').inputValue(), 'full');
  assert.equal(await page.locator('#ruleSample').inputValue(), info.item_key);
  await page.locator('#ruleLines').fill('4');
  assert.equal(await page.locator('#ruleSample').inputValue(), info.item_key);
  await page.locator('#rulePreview').click();
  await page.waitForFunction(() => document.querySelector('#ruleResult').textContent.includes('说明误差来源'));
  await page.screenshot({ path: path.join(output, 'desktop.png') });
  await page.locator('#ruleSave').click();
  await page.waitForFunction(() => document.querySelector('#ruleStatus').textContent.includes('已保存'));
  assert.equal(await page.locator('[data-rule-needed]').filter({ hasText: 'fixture课程 A' }).count(), 0);
  await page.locator('#ruleReset').click();
  await page.waitForFunction(() => document.querySelector('#ruleStatus').textContent.includes('已恢复'));
  assert.equal(await page.locator('[data-rule-needed]').filter({ hasText: 'fixture课程 A' }).count(), 1);
  await page.locator('#ruleClose').click();
  await open();
  assert.equal(await page.locator('[data-rule-needed]').filter({ hasText: 'fixture课程 A' }).count(), 1);
  await page.locator('[data-rule="physics"]').click();
  assert.match(await page.locator('#ruleHelp').innerText(), /提交图片/);
  assert.match(await page.locator('#ruleHelp').innerText(), /一页作为一张照片/);
  await page.locator('#ruleText').fill('第一周作业提交这里\n提交图片\n一页作为一张照片，正放，不要倒置。\n习题：11-10、11-11。');
  await page.locator('#rulePreview').click();
  await page.waitForSelector('#ruleRemoved:not([hidden])');
  const removed = await page.locator('#ruleRemovedText').innerText();
  assert.match(removed, /第一周作业提交这里/);
  assert.match(removed, /提交图片/);
  assert(!removed.includes('11-10'));
  await page.locator('#ruleRemovedText').scrollIntoViewIfNeeded();
  const removedBounds = await page.locator('#ruleRemovedText').boundingBox();
  assert(removedBounds.y >= 0 && removedBounds.y + removedBounds.height <= 800);
  await page.screenshot({ path: path.join(output, 'removed-quotes.png') });
  await page.locator('#ruleMode').selectOption('omit');
  assert.equal(await page.locator('#ruleLines').isDisabled(), true);
  await page.locator('#rulePreview').click();
  await page.waitForFunction(() => document.querySelector('#ruleResult').textContent.includes('手动补充'));
  assert(!(await page.locator('#ruleResult').innerText()).includes('第一周作业提交这里'));
  await page.locator('#ruleNew').click();
  await page.locator('#ruleCourse').fill('fixture课程 A');
  await page.locator('#ruleMode').selectOption('omit');
  await page.locator('#ruleSave').click();
  await page.waitForFunction(() => document.querySelector('#ruleStatus').textContent.includes('已保存'));
  await page.locator('#ruleClose').click();
  await page.locator('#supplementBtn').click();
  await page.waitForSelector('#dlgSupplement[open]');
  const image = await page.locator('#dlgSupplement').screenshot();
  await page.locator('#supplementFiles').setInputFiles({ name: '作业截图.png', mimeType: 'image/png', buffer: image });
  await page.waitForSelector('#supplementImages img');
  await page.locator('#supplementSave').click();
  await page.waitForSelector('#dlgSupplement', { state: 'hidden' });
  await page.locator('#pvBtn').click();
  await page.waitForFunction(() => document.querySelectorAll('#bubble img').length === 1);
  const message = await page.locator('#bubble').innerText();
  assert.match(message, /fixture作业（脱敏）/);
  assert.match(message, /⏰/);
  for (const body of ['详细要求见北化在线', '绘制测量曲线', '提交图片', '习题：10-11']) {
    assert(!message.includes(body), body + ' is removed from the reminder');
  }
  await page.screenshot({ path: path.join(output, 'supplement-only.png') });
  await page.locator('#pvClose').click();
  await open();
  await page.locator('#ruleList [data-rule]').filter({ hasText: 'fixture课程 A' }).click();
  assert.equal(await page.locator('#ruleMode').inputValue(), 'omit');
  await page.setViewportSize({ width: 520, height: 620 });
  await page.locator('#ruleMode').scrollIntoViewIfNeeded();
  assert.equal(await page.locator('#ruleMode').inputValue(), 'omit');
  const bounds = await page.locator('#ruleMask .modal').boundingBox();
  assert(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= 520 && bounds.y + bounds.height <= 620);
  assert(await page.locator('#ruleClose').isVisible());
  assert(await page.locator('#ruleSave').isVisible());
  await page.screenshot({ path: path.join(output, 'compact.png') });
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  console.log('Course rule reminder browser acceptance passed. External requests 0.');
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  clearTimeout(timer); await browser?.close(); child?.kill();
});
