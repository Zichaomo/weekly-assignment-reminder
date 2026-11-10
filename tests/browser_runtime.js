// Share the standalone Python environment and its bundled Playwright driver.
const { execFileSync } = require('node:child_process');
const os = require('node:os');
const path = require('node:path');

const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
const PYTHON = process.env.PYTHON || path.join(localAppData,
  'Programs', 'Python', 'Python314', 'python.exe');
const playwrightPath = process.env.PLAYWRIGHT_PATH || execFileSync(PYTHON, ['-B', '-c',
  'from pathlib import Path; import playwright; print(Path(playwright.__file__).parent / "driver" / "package")',
], { encoding: 'utf8', timeout: 10000, windowsHide: true,
  env: { ...process.env, PYTHONIOENCODING: 'utf-8' } }).trim();
const { chromium } = require(playwrightPath);

module.exports = { PYTHON, chromium };
