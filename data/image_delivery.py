"""Original-image delivery plans, shared by scheduled and console publishing."""
import hashlib
import io
import json
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
