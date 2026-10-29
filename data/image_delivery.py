"""Original-image delivery plans, shared by scheduled and console publishing."""
import hashlib
import io
import json
import re
import urllib.request
from urllib.parse import urlsplit, quote
from pathlib import Path

import cache_store
import content_summary


def collect(items):
    plan = []
    for item in items:
        if item.get('kind') in ('test', 'manual') or item.get('source') == 'manual':
            continue
        summary = content_summary.summarize(item)
        if summary['status'] != 'images':
            continue
        for aid in summary['asset_ids']:
            plan.append({k:item.get(k) for k in ('item_key', 'source_version', 'course', 'title', 'deadline')}
                        | {'asset_id':aid})
    return plan


def original_bytes(entry):
    item = cache_store.get_item(entry['item_key'])
    if not item or item.get('source_version') != entry['source_version']:
        raise ValueError('图片正文版本已变化')
    summary = content_summary.summarize(item)
    if summary['status'] != 'images' or entry['asset_id'] not in summary['asset_ids']:
        raise ValueError('图片已过期、获取失败或不再属于当前发送内容')
    record = cache_store.asset_record(entry['asset_id'])
    if not record:
        raise ValueError('原图缓存缺失')
    path = Path(record.get('original_path') or record['path']).resolve()
    path.relative_to(Path(cache_store.cache_settings()['assets_dir']).resolve())
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('原图超过企业微信图片大小限制')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['asset_id']:
        raise ValueError('原图文件校验失败')
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in ('JPEG', 'PNG'):
            raise ValueError('原图不是企业微信支持的 JPG/PNG')
        image.verify()
    return raw


def bundle_key(text, images):
    return hashlib.sha256(json.dumps([text, images], sort_keys=True,
                                    ensure_ascii=False).encode('utf-8')).hexdigest()


def text_chunks(text, limit=1800):
    chunks, current, size = [], '', 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size + width > limit:
            chunks.append(current)
            current, size = '', 0
        current += char
        size += width
    if current:
        chunks.append(current)
    return chunks


def inline_markdown(text, images):
    """Use only anonymously verified, byte-identical platform image URLs."""
    if not images:
        return None
    marker = r'^   作业原图随提醒发送（\d+ 张）。$'
    if len(re.findall(marker, text, re.M)) != len({e['item_key'] for e in images}):
        return None
    groups = {}
    try:
        for entry in images:
            raw = original_bytes(entry)
            record = cache_store.asset_record(entry['asset_id'])
            url = record['source_url']
            parts = urlsplit(url)
            if parts.scheme != 'https' or parts.hostname != 'course.buct.edu.cn' or parts.username:
                return None
            with urllib.request.urlopen(urllib.request.Request(url,
                    headers={'User-Agent':'Mozilla/5.0'}), timeout=10) as response:
                downloaded = response.read(len(raw)+1)
            if downloaded != raw:
                return None
            groups.setdefault(entry['item_key'], []).append((entry, quote(url, safe=':/?=&%')))
    except (ValueError, OSError):
        return None
    ordered = sorted(groups.values(), key=lambda group:text.find('｜'+group[0][0]['title']))
    if len(re.findall(marker, text, re.M)) != len(ordered):
        return None
    # Escape literal source text before introducing the image Markdown.
    body = re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', text)
    cursor = iter(ordered)
    body = re.sub(marker, lambda _: '\n\n' + '\n\n'.join(
        f'![作业原图]({url})' for entry, url in next(cursor)) + '\n', body, flags=re.M)
    # Keep each homework with its image when the API's 4096-byte limit requires
    # more than one message. An oversized individual item uses separate images.
    chunks, current = [], ''
    for block in re.split(r'(?=^• )', body, flags=re.M):
        if len(block.encode('utf-8')) > 4000:
            return None
        if current and len((current+block).encode('utf-8')) > 4000:
            chunks.append(current)
            current = ''
        current += block
    if current:
        chunks.append(current)
    return chunks
