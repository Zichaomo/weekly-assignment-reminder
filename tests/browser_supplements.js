// Real UI and API, isolated cache, no platform access or message delivery.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { PYTHON, chromium } = require('./browser_runtime');
let fixture, browser;
const output = path.join(__dirname, 'artifacts', 'supplements');
async function main() {
  fs.mkdirSync(output, { recursive: true });
  fixture = spawn(PYTHON, ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  const info = await new Promise((resolve, reject) => {
    let stdout = '', stderr = '';
    const timer = setTimeout(() => reject(Error('Fixture timeout ' + stderr)), 15000);
    fixture.stderr.on('data', data => { stderr += data; });
    fixture.stdout.on('data', data => {
      stdout += data;
      const line = stdout.split(/\r?\n/).find(value => value.startsWith('{'));
      if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
    });
    fixture.on('exit', () => { clearTimeout(timer); reject(Error(stderr)); });
  });
  const origin = 'http://127.0.0.1:' + info.port;
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const errors = [], external = [];
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin !== origin) {
      external.push(route.request().url()); return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(origin);
  await page.locator('#list .item').filter({ hasText: 'fixture通用说明' }).click();
  await page.locator('#supplementBtn').click();
  await page.waitForSelector('#dlgSupplement[open]');
  await page.locator('#supplementText').fill('PPT 第 12 页\n完成 1-3、1-5');
  const screenshot = await page.locator('#dlgSupplement').screenshot();
  await page.locator('#supplementFiles').setInputFiles({ name: 'PPT.png', mimeType: 'image/png', buffer: screenshot });
  await page.waitForSelector('#supplementImages img');
  // Clipboard paste follows the same real file ingestion path.
  await page.locator('#supplementText').evaluate(element => {
    const canvas = document.createElement('canvas'); canvas.width = 100; canvas.height = 60;
    const data = atob(canvas.toDataURL('image/png').split(',')[1]);
    const file = new File([Uint8Array.from(data, c => c.charCodeAt(0))], '粘贴截图.png', { type: 'image/png' });
    const transfer = new DataTransfer(); transfer.items.add(file);
    element.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
  });
  await page.waitForFunction(() => document.querySelectorAll('#supplementImages img').length === 2);
  await page.locator('#supplementSave').click();
  await page.waitForSelector('#dlgSupplement', { state: 'hidden' });
  await page.waitForSelector('#assignmentSupplement img');
  assert.match(await page.locator('#assignmentSupplement').innerText(), /PPT 第 12 页/);
  assert.equal(await page.locator('#assignmentSupplement img').count(), 2);
  await page.locator('#pvBtn').click();
  await page.waitForFunction(() => document.querySelectorAll('#bubble img').length === 2);
  assert.match(await page.locator('#bubble').innerText(), /PPT 第 12 页/);
  const previewLines = (await page.locator('#bubble').textContent()).split('\n');
  const details = previewLines.filter(line => /⏰|PPT 第 12 页|完成 1-3、1-5|详细要求见北化在线/.test(line));
  assert(details.length >= 5);
  assert(details.every(line => line.startsWith('\u3000\u3000')), 'message details have visible indentation');
  assert(!/补充图片随提醒发送/.test(await page.locator('#bubble').innerText()));
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 700 }]) {
    await page.setViewportSize(viewport);
    await page.screenshot({ path: path.join(output, `message-preview-${viewport.width}.png`), animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.locator('#pvClose').click();
  await page.getByRole('button', { name: '不推本条作业', exact: true }).click();
  await page.locator('#dlgConfirm #cfOk').click();
  await page.waitForSelector('#dlgConfirm[open]', { state: 'hidden' });
  const excluded = page.locator('#list .item').filter({ hasText: 'fixture通用说明' });
  assert.match(await excluded.innerText(), /不推送/);
  assert.match(await page.locator('#detail .flags').innerText(), /已跳过班级和个人提醒/);
  await page.reload();
  await excluded.click();
  assert.match(await excluded.innerText(), /不推送/);
  assert.equal(await page.locator('#assignmentSupplement img').count(), 2);
  await page.locator('#pvBtn').click();
  await page.waitForFunction(() => !document.querySelector('#copyBtn').disabled);
  for (const audience of ['class', 'personal']) {
    await page.locator('#seg button[data-v="' + audience + '"]').click();
    assert.doesNotMatch(await page.locator('#bubble').innerText(), /fixture通用说明|PPT 第 12 页/);
    assert.match(await page.locator('#bubble').innerText(), /fixture有效摘要/);
    assert.equal(await page.locator('#bubble img').count(), 0);
  }
  await page.locator('#pvClose').click();
  await page.setViewportSize({ width: 390, height: 700 });
  const restore = page.getByRole('button', { name: '恢复推送', exact: true });
  const restoreBox = await restore.boundingBox();
  assert(restoreBox && restoreBox.x >= 0 && restoreBox.y >= 0);
  assert(restoreBox.x + restoreBox.width <= 391 && restoreBox.y + restoreBox.height <= 701);
  await page.screenshot({ path: path.join(output, 'reminder-excluded-small.png'), animations: 'disabled' });
  await restore.click();
  await page.getByRole('button', { name: '不推本条作业', exact: true }).waitFor();
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.locator('#pvBtn').click();
  await page.waitForFunction(() => document.querySelectorAll('#bubble img').length === 2);
  assert.match(await page.locator('#bubble').innerText(), /fixture通用说明/);
  await page.locator('#pvClose').click();
  // Same titles in different courses and both image types stay with their item.
  const previewOrder = await page.evaluate(() => {
    const host = document.createElement('div');
    renderPreviewMessage(host,
      '• 甲课｜第一周作业\n   作业原图随提醒发送（1 张）。\n   补充图片随提醒发送（1 张）。\n• 乙课｜第一周作业\n   补充图片随提醒发送（1 张）。', [
        { item_key: 'b', course: '乙课', title: '第一周作业', asset_id: 'b', supplement: true },
        { item_key: 'a', course: '甲课', title: '第一周作业', asset_id: 'a' },
        { item_key: 'a', course: '甲课', title: '第一周作业', asset_id: 'c', supplement: true },
      ]);
    return [...host.querySelectorAll('img')].map(image => image.getAttribute('src'));
  });
  assert.deepEqual(previewOrder, ['/api/assets/a', '/api/assets/c', '/api/assets/b']);
  await page.reload();
  await page.locator('#list .item').filter({ hasText: 'fixture通用说明' }).click();
  assert.equal(await page.locator('#assignmentSupplement img').count(), 2);
  await page.locator('#supplementBtn').click();
  await page.waitForSelector('#dlgSupplement[open]');
  assert.equal(await page.locator('#supplementText').inputValue(), 'PPT 第 12 页\n完成 1-3、1-5');
  for (const viewport of [{ width: 1280, height: 800 }, { width: 900, height: 640 }, { width: 390, height: 700 }, { width: 640, height: 360 }]) {
    await page.setViewportSize(viewport);
    const box = await page.locator('#dlgSupplement').boundingBox();
    assert(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1);
    assert(box.y + box.height <= viewport.height + 1);
    await page.locator('#supplementSave').scrollIntoViewIfNeeded();
    const button = await page.locator('#supplementSave').boundingBox();
    assert(button.y >= 0 && button.y + button.height <= viewport.height + 1);
    assert(await page.locator('#dlgSupplement').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.screenshot({ path: path.join(output, `editor-${viewport.width}-${viewport.height}.png`), animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.locator('#supplementImages [data-remove-supplement]').first().click();
  await page.locator('#supplementText').fill('修改后的补充');
  // A second editor saves first. The first editor must retain its own draft.
  const conflict = await page.evaluate(async () => {
    const key = document.querySelector('#list .item[aria-selected="true"]').dataset.key;
    const url = '/api/assignments/' + encodeURIComponent(key) + '/supplement';
    const data = await (await fetch(url)).json();
    return (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_revision: data.supplement.revision,
        expected_identity_generation: data.supplement.identity_generation, text: '另一页保存', images: [] }) })).status;
  });
  assert.equal(conflict, 200);
  await page.locator('#supplementSave').click();
  await page.waitForFunction(() => document.querySelector('#supplementStatus').textContent.includes('未保存'));
  assert.equal(await page.locator('#supplementText').inputValue(), '修改后的补充');
  assert.equal(await page.locator('#supplementImages img').count(), 1);
  await page.locator('#supplementClose').click();
  await page.locator('#supplementBtn').click();
  await page.waitForSelector('#dlgSupplement[open]');
  assert.equal(await page.locator('#supplementText').inputValue(), '修改后的补充');
  assert.match(await page.locator('#supplementLatest').innerText(), /另一页保存/);
  await page.locator('#supplementSave').click();
  await page.waitForSelector('#dlgSupplement', { state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('#assignmentSupplement').textContent.includes('修改后的补充'));
  assert.equal(await page.locator('#assignmentSupplement img').count(), 1);
  await page.reload();
  await page.locator('#list .item').filter({ hasText: 'fixture通用说明' }).click();
  await page.locator('#supplementBtn').click();
  await page.waitForSelector('#dlgSupplement[open]');
  await page.locator('#supplementClear').click();
  await page.waitForSelector('#dlgSupplement', { state: 'hidden' });
  assert.equal(await page.locator('#assignmentSupplement img').count(), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  console.log('Supplement acceptance passed. Upload, paste, persistence, edit, conflict, clear and four viewports.');
}
main().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(async () => { await browser?.close(); fixture?.kill(); });
