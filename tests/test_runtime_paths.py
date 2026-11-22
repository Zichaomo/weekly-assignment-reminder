"""Regression coverage for the standalone Windows Python runtime and profile."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import DATA, ROOT

import edge_cred
import login
import scrape
import task_register


class RuntimePathTests(unittest.TestCase):
    def test_windows_launchers_keep_python_314_as_the_preferred_default(self):
        launcher = (DATA / 'launch_with_python.ps1').read_text(encoding='utf-8')
        self.assertIn('Programs\\Python\\Python314\\python.exe', launcher)
        for name in ('console.bat', 'login.bat', 'setup.bat'):
            with self.subTest(name=name):
                source = (ROOT / name).read_text(encoding='utf-8')
                self.assertIn('launch_with_python.ps1', source, name)
                self.assertFalse('.work' + 'buddy' in source.lower(), name)
        source = (DATA / 'console.ps1').read_text(encoding='utf-8')
        self.assertTrue('$env:LOCALAPPDATA' in source)
        self.assertTrue('Programs\\Python\\Python314\\python.exe' in source)

    def test_cli_schedule_registration_keeps_current_interpreter(self):
        current = str(ROOT / 'test python' / 'python.exe')
        with patch.object(task_register.sys, 'executable', current):
            self.assertEqual(task_register.find_python(), current)

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_windows_launchers_honor_selected_interpreter_and_auto_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            shutil.copyfile(DATA / 'launch_with_python.ps1', data / 'launch_with_python.ps1')
            config = root / 'config.json'
            marker = root / 'selected.txt'
            selected = root / '自选环境 ñ 🚀' / 'python.exe'
            selected.parent.mkdir()
            selected.touch()
            default = root / 'Programs' / 'Python' / 'Python314' / 'python.exe'
            default.parent.mkdir(parents=True)
            default.touch()
            env = dict(os.environ, LOCALAPPDATA=str(root), HOMEWORK_CONFIG_PATH=str(config),
                       RUNTIME_TEST_MARKER=str(marker))
            env.pop('HOMEWORK_LAUNCH_PYTHON', None)
            for name in ('console', 'setup', 'login'):
                source = (ROOT / (name + '.bat')).read_text(encoding='utf-8')
                stub = source.split('set "PY=', 1)[0] + (
                    'powershell -NoProfile -Command '
                    '"[IO.File]::WriteAllText($env:RUNTIME_TEST_MARKER,$env:HOMEWORK_LAUNCH_PYTHON)"\n'
                    'exit /b %ERRORLEVEL%\n'
                )
                launcher = root / (name + '.bat')
                launcher.write_text(stub, encoding='ascii')
                for saved, expected in [(str(selected), selected), ('', default),
                                        (str(root / 'missing' / 'python.exe'), default)]:
                    with self.subTest(name=name, expected=expected):
                        config.write_text(json.dumps({'python_path': saved}, ensure_ascii=False), encoding='utf-8')
                        marker.unlink(missing_ok=True)
                        result = subprocess.run(
                            ['cmd', '/d', '/c', str(launcher)], cwd=root, env=env,
                            capture_output=True, timeout=20,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(marker.read_text(encoding='utf-8'), str(expected))

    def test_login_scraping_and_credentials_share_project_profile(self):
        for module in (login, scrape, edge_cred):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.PROFILE_DIR, DATA / 'browser-profile')
        self.assertEqual(edge_cred.LOCAL_STATE, DATA / 'browser-profile' / 'Local State')
        self.assertEqual(edge_cred.LOGIN_DB, DATA / 'browser-profile' / 'Default' / 'Login Data')

    def test_profile_and_local_archive_are_ignored(self):
        paths = ['data/browser-profile/Default/Login Data',
                 'data/browser-profile/Local State', '.local-state/legacy-session/memory/file.md']
        result = subprocess.run(
            ['git', 'check-ignore', '--no-index', '-z', '--stdin'],
            input='\0'.join(paths) + '\0', capture_output=True, text=True,
            encoding='utf-8', cwd=ROOT, timeout=10,
        )
        self.assertEqual(set(result.stdout.strip('\0').split('\0')), set(paths))

    def test_browser_tests_use_shared_runtime_and_installed_playwright(self):
        for name in ('browser_smoke.js', 'browser_v0.js', 'browser_v0_acceptance.js',
                     'browser_deadline.js', 'browser_run_history.js'):
            with self.subTest(name=name):
                source = (ROOT / 'tests' / name).read_text(encoding='utf-8')
                self.assertTrue("require('./browser_runtime')" in source, name)
        result = subprocess.run(
            ['node', '-e', "const r=require('./tests/browser_runtime');"
             "console.log(JSON.stringify({python:r.PYTHON,chromium:!!r.chromium.launch}));"],
            env=dict(os.environ, PYTHON=sys.executable), cwd=ROOT,
            capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'python': sys.executable, 'chromium': True})

    def test_project_sources_have_no_retired_host_references(self):
        paths = [ROOT / '.gitignore', ROOT / 'README.md']
        paths += list((ROOT / 'docs').glob('*.md'))
        paths += list(DATA.glob('*.py')) + list(DATA.glob('*.ps1'))
        paths += list(ROOT.glob('*.bat')) + list((ROOT / 'tests').glob('*.js'))
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertFalse('work' + 'buddy' in path.read_text(encoding='utf-8').lower(), str(path))

    def test_browser_driver_discovery_uses_utf8_for_chinese_install_paths(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('./tests/browser_runtime.js', 'utf8');
function load(name) {
  if (name === 'node:child_process') return {
    execFileSync(python, args, options) {
      assert.equal(python, 'test-python');
      assert.equal(options.env?.PYTHONIOENCODING, 'utf-8');
      assert.equal(options.env.RUNTIME_TEST, 'kept');
      return 'mock-playwright\n';
    },
  };
  if (name === 'mock-playwright') return { chromium: { launch() {} } };
  return require(name);
}
vm.runInNewContext(source, { require: load, module: { exports: {} }, process: {
  env: { PYTHON: 'test-python', PYTHONIOENCODING: 'ascii', RUNTIME_TEST: 'kept' },
} });
"""
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
