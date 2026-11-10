// Till deadline acceptance: layout and v0 colors, using the real UI and an isolated API.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { PYTHON, chromium } = require('./browser_runtime');
const root = path.resolve(__dirname, '..');
const output = path.join(__dirname, 'artifacts', 'deadline');
const checks = [];
const errors = [];
const externalRequests = [];
let fixture, browser, timeout;

async function check(name, run) {
  try { await run(); checks.push({ name, ok: true }); }
  catch (error) { checks.push({ name, ok: false, error: error.message }); }
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  timeout = setTimeout(() => { fixture?.kill(); browser?.close(); process.exitCode = 1; }, 90000);
  fixture = spawn(PYTHON,
  ['-B', path.join(__dirname, 'browser_fixture_server.py')], {
    cwd: root, env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, windowsHide: true,
  });
  const info = await new Promise((resolve, reject) => {
    let stdout = '', stderr = '';
    const limit = setTimeout(() => reject(Error('Fixture timeout: ' + stderr)), 15000);
    fixture.stderr.on('data', data => { stderr += data; });
    fixture.stdout.on('data', data => {
      stdout += data;
      const line = stdout.split(/\r?\n/).find(value => value.startsWith('{'));
      if (line) { clearTimeout(limit); resolve(JSON.parse(line)); }
    });
    fixture.on('exit', () => { clearTimeout(limit); reject(Error(stderr || 'Fixture exited')); });
  });
  const origin = 'http://127.0.0.1:' + info.port;
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 },
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin !== origin) {
      externalRequests.push(route.request().url()); return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(3000);
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.setFixedTime(new Date('2026-11-10T21:54:00+08:00'));
  const snapshot = await page.request.get(origin + '/api/list').then(response => response.json());
  const platform = snapshot.rows.find(row => row.item_key === info.item_key);
  assert.ok(platform, 'Use a platform item from the real local-cache API');
  const rows = [
    { ...platform, title: 'Till 今天截止', deadline: '2026-11-10 23:59' },
    { ...platform, item_key: 'deadline-tomorrow', title: 'Till 明天截止', deadline: '2026-11-11 18:30' },
    { source: '手动', item_key: 'manual:301', manual_id: 301, course: '补录课程', title: 'Till 稍后截止', deadline: '2026-11-13 20:00' },
    { source: '手动', item_key: 'manual:302', manual_id: 302, course: '补录课程', title: 'Till 已逾期', deadline: '2026-11-10 20:30' },
    { source: '手动', item_key: 'manual:303', manual_id: 303, course: '补录课程', title: 'Till 未设截止', deadline: '' },
  ];
  await page.route('**/api/list?*', route => route.fulfill({ json: { ...snapshot, rows } }));
  await page.goto(origin);
  await page.waitForSelector('#list .item');
  async function select(title) { await page.locator('#list .item').filter({ hasText: title }).click(); }
  await select('Till 今天截止');

  await check('chosen Till dual-column card places a full-width thin bar under both values', async () => {
    assert.equal(await page.locator('.deadline > .deadline-info').count(), 1);
    assert.equal(await page.locator('.deadline-info > .count').count(), 1);
    assert.equal(await page.locator('.deadline > .timeline').count(), 1);
    const layout = await page.locator('.deadline').evaluate(element => {
      const info = element.querySelector('.deadline-info').getBoundingClientRect();
      const bar = element.querySelector('.bar').getBoundingClientRect();
      const number = element.querySelector('.count .deadline-number');
      return { below: bar.top > info.bottom, width: bar.width, infoWidth: info.width,
        height: bar.height, radius: getComputedStyle(element).borderRadius,
        numberSize: number ? parseFloat(getComputedStyle(number).fontSize) : 0 };
    });
    assert.ok(layout.below);
    assert.ok(Math.abs(layout.width - layout.infoWidth) < 1);
    assert.equal(layout.height, 4);
    assert.equal(layout.radius, '17px');
    assert.ok(layout.numberSize >= 35);
    assert.equal(await page.locator('.deadline .scale').count(), 0);
    assert.equal(await page.locator('.deadline time').innerText(), '11月10日 周二 23:59');
    assert.equal(await page.locator('.count .k').innerText(), '还剩');
    assert.equal(await page.locator('.count .v').innerText(), '2 小时 5 分');
  });

  for (const [title, heat, barColor, countColor] of [
    ['Till 今天截止', 'hot', 'rgb(177, 83, 47)', 'rgb(177, 83, 47)'],
    ['Till 明天截止', 'warm', 'rgb(199, 176, 138)', 'rgb(138, 109, 62)'],
    ['Till 稍后截止', '', 'rgb(90, 107, 67)', 'rgb(35, 37, 31)'],
  ]) {
    await check(title + ' retains v0 bar and countdown colors', async () => {
      await select(title);
      const colors = await page.locator('.deadline').evaluate(element => ({
        bar: getComputedStyle(element.querySelector('.bar i')).backgroundColor,
        count: getComputedStyle(element.querySelector('.count .v')).color,
        digits: [...element.querySelectorAll('.count .deadline-number')].map(number => getComputedStyle(number).color),
      }));
      assert.equal(colors.bar, barColor); assert.equal(colors.count, countColor);
      if (heat) assert.equal(await page.locator('.bar').evaluate((element, value) => element.classList.contains(value), heat), true);
      assert.ok(colors.digits.length > 0 && colors.digits.every(color => color === countColor));
    });
  }
  await check('overdue and undated items keep truthful, nonduplicated states', async () => {
    await page.locator('#chkAll').check();
    await select('Till 已逾期');
    assert.equal(await page.locator('.count .k').innerText(), '状态');
    assert.equal(await page.locator('.count .v').innerText(), '已逾期');
    assert.equal(await page.locator('.bar').getAttribute('aria-valuenow'), '100');
    await select('Till 未设截止');
    assert.equal(await page.locator('.deadline').innerText(), '截止时间\n未设截止');
    assert.equal(await page.locator('.deadline .timeline, .deadline .count').count(), 0);
  });
  await check('real platform and manual details fit desktop and narrow viewports', async () => {
    for (const [width, height] of [[1440, 900], [1280, 800], [1100, 600], [800, 800], [390, 900], [320, 900]]) {
      await page.setViewportSize({ width, height });
      for (const title of ['Till 今天截止', 'Till 稍后截止']) {
        await select(title);
        await page.locator('.deadline').scrollIntoViewIfNeeded();
        const layout = await page.locator('.deadline').evaluate(element => {
          const rect = element.getBoundingClientRect();
          const detail = document.querySelector('#detail').getBoundingClientRect();
          const pieces = [...element.querySelectorAll('time, .count, .bar')].map(child => {
            const childRect = child.getBoundingClientRect();
            return { left: childRect.left, right: childRect.right, top: childRect.top, bottom: childRect.bottom };
          });
          return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
            contentWidth: element.scrollWidth, width: element.clientWidth,
            viewTop: Math.max(0, detail.top), viewBottom: Math.min(innerHeight, detail.bottom), pieces };
        });
        assert.ok(layout.left >= 0 && layout.right <= width + 1, JSON.stringify({ width, layout }));
        assert.ok(layout.contentWidth <= layout.width + 1, 'Deadline must not overflow');
        assert.ok(layout.top >= layout.viewTop - 1 && layout.bottom <= layout.viewBottom + 1, JSON.stringify({ width, layout }));
        for (const piece of layout.pieces) {
          assert.ok(piece.left >= layout.left && piece.right <= layout.right + 1, JSON.stringify({ width, piece, layout }));
          assert.ok(piece.top >= layout.top && piece.bottom <= layout.bottom + 1);
        }
      }
      await page.screenshot({ path: path.join(output, 'deadline-' + width + '.png') });
    }
  });
  await check('focus refresh updates countdown, progress and day color without replacing body images', async () => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await select('Till 今天截止');
    const image = await page.locator('#itemImages img').first().elementHandle();
    await page.clock.setFixedTime(new Date('2026-11-10T21:59:00+08:00'));
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await page.locator('.count .v').innerText(), '2 小时 0 分');
    assert.equal(await image.evaluate(element => element.isConnected), true);
    assert.equal(await page.locator('.bar').getAttribute('role'), 'progressbar');
    assert.match(await page.locator('.bar').getAttribute('aria-valuetext'), /2 小时 0 分/);
    await select('Till 明天截止');
    await page.clock.setFixedTime(new Date('2026-11-11T00:01:00+08:00'));
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(await page.locator('.bar.hot').count(), 1);
    assert.equal(await page.locator('.count .v.hot').count(), 1);
  });
  assert.deepEqual(errors, []); assert.deepEqual(externalRequests, []);
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ checks, errors, externalRequests }, null, 2));
  const failures = checks.filter(check => !check.ok);
  console.log(JSON.stringify({ passed: checks.length - failures.length, failed: failures }, null, 2));
  if (failures.length) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  clearTimeout(timeout); await browser?.close(); fixture?.kill();
});
