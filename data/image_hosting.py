"""Publish selected image bytes when the local configuration enables hosting."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit
import uuid

import cache_store
from platform_lock import FetchLock, LockTimeout

PROVIDER = 'freeimage.host'
DOCUMENTATION_URL = 'https://freeimage.host/api'
UPLOAD_URL = 'https://freeimage.host/api/1/upload'


class HostingError(ValueError):
    pass


def _matches(url, raw):
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return False
        import notify
        request = urllib.request.Request(url, headers={'User-Agent': notify.UA})
        with notify._open_delivery_request(request, timeout=10) as response:
            return response.read(len(raw) + 1) == raw
    except (ValueError, OSError):
        return False


class _UploadParameter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get('value', '')
        if tag == 'input' and 'readonly' in attrs and 16 <= len(value) <= 128:
            self.values.append(value)


def _upload(raw):
    import notify
    try:
        request = urllib.request.Request(DOCUMENTATION_URL, headers={'User-Agent': notify.UA})
        with notify._open_delivery_request(request, timeout=15) as response:
            html = response.read(300000).decode('utf-8')
        # The service documents a shared public upload parameter. Keep it only
        # in memory, outside configuration, diagnostics, and saved receipts.
        section = html.split('API Key', 1)[1].split('<h2', 1)[0]
        parameter = _UploadParameter()
        parameter.feed(section)
        if len(parameter.values) != 1:
            raise HostingError('图片托管接口参数读取失败，请稍后重试或检查服务文档')
        boundary = 'HomeworkUpload' + uuid.uuid4().hex
        fields = [('key', parameter.values[0]), ('action', 'upload'), ('format', 'json')]
        parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                  f'\r\n\r\n{value}\r\n').encode() for name, value in fields]
        suffix, mime = ('png', 'image/png') if raw.startswith(b'\x89PNG\r\n\x1a\n') else ('jpg', 'image/jpeg')
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="source"; '
                      f'filename="homework.{suffix}"\r\nContent-Type: {mime}\r\n\r\n').encode()
                     + raw + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        request = urllib.request.Request(UPLOAD_URL, data=b''.join(parts),
            headers={'User-Agent': notify.UA, 'Content-Type': f'multipart/form-data; boundary={boundary}'},
            method='POST')
        with notify._open_delivery_request(request, timeout=30) as response:
            result = json.loads(response.read(200000))
        if result.get('status_code') != 200:
            raise HostingError('图片托管服务未接受图片，请稍后重试')
        url = result['image']['url']
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in ('iili.io', PROVIDER)
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise HostingError('图片托管服务返回了无效的图片链接')
        if not _matches(url, raw):
            raise HostingError('图片托管后的图片校验失败，请稍后重试')
        return url
    except HostingError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        # Remote responses and exceptions can contain request parameters.
        raise HostingError('图片托管请求失败，请检查网络后重试') from None


def image_url(entry, raw, settings=None):
    asset_id = entry.get('delivery_asset_id') or entry['asset_id']
    record = cache_store.asset_record(asset_id) or {}
    previous_url = record.get('source_url')
    if _matches(previous_url, raw):
        return previous_url
    if not isinstance(settings, dict) or settings.get('provider') != PROVIDER:
        return None
    if hashlib.sha256(raw).hexdigest() != asset_id:
        raise HostingError('图片托管前的图片校验失败，请重新预览')
    lock_path = Path(cache_store.cache_settings()['assets_dir']) / f'hosting-{asset_id}.lock'
    try:
        with FetchLock(path=lock_path, wait_sec=60):
            record = cache_store.asset_record(asset_id) or {}
            current_url = record.get('source_url')
            if current_url != previous_url and _matches(current_url, raw):
                return current_url
            import image_delivery
            if image_delivery.delivery_bytes(entry) != raw:
                raise HostingError('图片托管前作业图片已变化，请重新预览')
            url = _upload(raw)
            with cache_store.connection(write=True) as con:
                updated = con.execute('UPDATE assets SET source_url=? WHERE asset_id=? AND sha256=?',
                                      (url, asset_id, asset_id)).rowcount
                if updated != 1:
                    raise HostingError('图片缓存已变化，请重新预览')
            return url
    except LockTimeout:
        raise HostingError('图片托管正被其他发送任务使用，请稍后重试') from None
