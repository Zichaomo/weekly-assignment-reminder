"""Atomic console settings and Windows-user-protected school credentials."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()
CHANNELS = ('clipboard', 'wecom', 'pushplus', 'wechat', 'none')
HOOKS = ('wecom_webhook', 'wecom_webhook_test', 'wecom_webhook_prod', 'personal_webhook')


class Conflict(ValueError):
    pass


class Blob(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('账号加密保存需要 Windows')
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    free = ctypes.WinDLL('kernel32').LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('无法使用当前 Windows 用户加解密凭据，请重新录入账号密码')
    try:
        return ctypes.string_at(target.data, target.length)
    finally:
        free(target.data)


def config_path():
    return Path(os.environ.get('HOMEWORK_CONFIG_PATH') or ROOT / 'config.json')


def _read():
    try:
        raw = config_path().read_bytes()
    except FileNotFoundError:
        raw = b''
    try:
        cfg = json.loads(raw) if raw else {'channel': 'clipboard', 'setup_completed': False}
    except (ValueError, UnicodeError):
        raise ValueError('本机配置格式异常，请从备份恢复后再保存') from None
    if not isinstance(cfg, dict):
        raise ValueError('本机配置格式异常')
    return cfg, hashlib.sha256(raw).hexdigest()


def _write(cfg):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='settings-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(cfg, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _edit(payload):
    if not isinstance(payload, dict):
        raise ValueError('设置格式错误')
    cfg, revision = _read()
    if payload.get('expected_revision') != revision:
        raise Conflict('设置已在其他页面更新，请重新打开后再保存')
    return cfg


def _account(cfg):
    protected = (cfg.get('school_account') or {}).get('protected')
    if not protected:
        return {}
    try:
        account = json.loads(_protect(base64.b64decode(protected, validate=True), decrypt=True))
        if not isinstance(account, dict) or not account.get('username') or not account.get('password'):
            raise ValueError()
        return account
    except (ValueError, TypeError, KeyError):
        raise ValueError('已保存凭据无法读取，请重新填写账号和密码') from None


def credentials():
    with LOCK:
        account = _account(_read()[0])
        return account.get('username'), account.get('password')


def needs_login():
    return bool((_read()[0].get('school_account') or {}).get('needs_login'))


def account_status():
    with LOCK:
        cfg, revision = _read()
        error = ''
        try:
            account = _account(cfg)
        except ValueError as exc:
            account, error = {}, str(exc)
        managed = cfg.get('school_account') or {}
        return {'revision': revision, 'username': account.get('username', ''),
                'password_saved': bool(managed.get('protected')), 'error': error,
                'needs_login': bool(managed.get('needs_login')),
                'session_saved': (config_path().parent / 'storage_state.json').is_file(),
                'source': managed.get('source', 'edge-or-manual')}


def save_account(payload):
    with LOCK:
        cfg = _edit(payload)
        import cache_store
        old_scope = cache_store.account_scope()
        action = payload.get('action', 'save')
        if action == 'clear':
            cfg['school_account'] = {'needs_login': True}
        else:
            try:
                old = _account(cfg)
            except ValueError:
                old = {}
            if action == 'import-edge':
                import edge_cred
                username, password = edge_cred.get_saved_credentials(verbose=False)
                if not username or not password:
                    raise ValueError('未找到可导入的 Edge 凭据，请在此录入账号密码')
            elif action == 'save':
                username = payload.get('username', '')
                password = payload.get('password', '')
            else:
                raise ValueError('未知的账号操作')
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError('账号与密码格式错误')
            username = username.strip()
            if not username or len(username) > 160 or len(password) > 4096:
                raise ValueError('请填写有效的学校账号')
            changed = bool(old.get('username') and old['username'] != username)
            if not password and not changed:
                password = old.get('password', '')
            if not password:
                raise ValueError('首次保存或更换账号时需要填写密码')
            if changed or payload.get('new_account') is True:
                cfg['account_scope'] = 'school-' + hashlib.sha256(username.encode()).hexdigest()[:16]
            blob = _protect(json.dumps({'username': username, 'password': password}).encode())
            cfg['school_account'] = {'protected': base64.b64encode(blob).decode('ascii'),
                                     'needs_login': True, 'source': action,
                                     'courses_pending': changed or payload.get('new_account') is True}
        _write(cfg)
        cache_store.invalidate_scope_scans('学校账号设置已更新，请重新登录并检索', scope=old_scope,
                                           status='auth_failed')
        return account_status()


def mark_logged_in():
    with LOCK:
        cfg, _ = _read()
        if 'school_account' in cfg:
            cfg['school_account']['needs_login'] = False
            _write(cfg)


def mark_courses_refreshed():
    with LOCK:
        cfg, _ = _read()
        if (cfg.get('school_account') or {}).get('courses_pending'):
            cfg['school_account']['courses_pending'] = False
            _write(cfg)


def wecom_status():
    cfg, revision = _read()
    personal = cfg.get('personal') or {}
    return {'revision': revision, 'channel': cfg.get('channel', 'clipboard'),
            'configured': {key: bool(personal.get('wecom_webhook') if key == 'personal_webhook'
                                     else cfg.get(key)) for key in HOOKS},
            'personal_channel': personal.get('channel', 'wecom'),
            'personal_enabled': personal.get('enabled', bool(personal.get('wecom_webhook'))),
            'environment_override': bool(os.environ.get('BUCT_WECOM_WEBHOOK'))}


def _validate_hook(value):
    try:
        url = urlsplit(value)
        valid = (url.scheme == 'https' and url.hostname == 'qyapi.weixin.qq.com'
                 and url.path == '/cgi-bin/webhook/send' and url.port in (None, 443)
                 and not url.username and not url.password and not url.fragment
                 and set(parse_qs(url.query)) == {'key'} and len(parse_qs(url.query)['key']) == 1)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('请填写有效的企业微信群机器人 Webhook 地址')


def save_wecom(payload):
    with LOCK:
        cfg = _edit(payload)
        channel = payload.get('channel', cfg.get('channel', 'clipboard'))
        if channel not in CHANNELS:
            raise ValueError('推送渠道无效')
        personal = dict(cfg.get('personal') or {})
        for key in HOOKS:
            value = payload.get(key, '')
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError('推送地址格式错误')
            value = value.strip()
            if payload.get('clear_' + key) is True:
                value = ''
            elif not value:
                continue
            if value:
                _validate_hook(value)
            if key == 'personal_webhook':
                personal['wecom_webhook'] = value
            else:
                cfg[key] = value
        if 'personal_enabled' in payload:
            if type(payload['personal_enabled']) is not bool:
                raise ValueError('个人提醒开关格式错误')
            personal['enabled'] = payload['personal_enabled']
        if 'personal_channel' in payload:
            if payload['personal_channel'] not in CHANNELS:
                raise ValueError('个人推送渠道无效')
            personal['channel'] = payload['personal_channel']
        cfg.update(channel=channel)
        if personal:
            cfg['personal'] = personal
        if channel == 'wecom' and not cfg.get('wecom_webhook'):
            raise ValueError('启用企业微信前请填写主用推送地址')
        if personal.get('enabled') and personal.get('channel') == 'wecom' and not personal.get('wecom_webhook'):
            raise ValueError('启用个人企业微信提醒前请填写个人推送地址')
        if personal.get('enabled') and personal.get('channel') == 'wecom':
            import notify
            ready, reason = notify.personal_ready(cfg)
            if not ready:
                raise ValueError(reason)
        _write(cfg)
        return wecom_status()


def setup_status():
    cfg, revision = _read()
    directory = config_path().parent
    try:
        courses = json.loads((directory / 'courses.json').read_text(encoding='utf-8')).get('courses', {})
    except (OSError, ValueError, AttributeError):
        courses = {}
    if (cfg.get('school_account') or {}).get('courses_pending'):
        courses = {}
    return {'revision': revision, 'needs_setup': not config_path().exists() or cfg.get('setup_completed') is False,
            'python': sys.version.split()[0], 'python_path': sys.executable,
            'playwright': importlib.util.find_spec('playwright') is not None,
            'pillow': importlib.util.find_spec('PIL') is not None,
            'edge': any((Path(base) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe').is_file()
                        for base in (os.environ.get('ProgramFiles(x86)', ''),
                                     os.environ.get('ProgramFiles', ''), os.environ.get('LOCALAPPDATA', '')) if base),
            'account': account_status(), 'wecom': wecom_status(), 'course_count': len(courses)}


def complete_setup(payload):
    with LOCK:
        cfg = _edit(payload)
        cfg['setup_completed'] = True
        _write(cfg)
        return setup_status()
