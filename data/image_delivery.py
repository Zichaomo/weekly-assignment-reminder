"""Version-bound original/crop plans shared by console and scheduled publishing."""
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
            base = {k:item.get(k) for k in ('item_key', 'source_version', 'course', 'title', 'deadline')}
            view = item.get('view') or {}
            regions = [r for r in view.get('regions', []) if r['asset_id'] == aid]
            if regions:
                for region in regions:
                    delivery_id = region.get('delivery_asset_id')
                    if not delivery_id:
                        # Upgrade pre-existing local views lazily, without overwriting
                        # their revision or modifying the preserved original.
                        import crop_assets
                        record = cache_store.asset_record(aid)
                        if not record:
                            raise ValueError('裁剪原图缓存缺失')
                        asset = crop_assets.generate(record, region['box'])
                        delivery_id = asset['asset_id']
                        with cache_store.connection(write=True) as con:
                            con.execute('''INSERT OR IGNORE INTO assets
                                (asset_id,sha256,path,mime,width,height,bytes,checked_at)
                                VALUES(?,?,?,?,?,?,?,0)''', tuple(asset[k] for k in
                                ('asset_id','sha256','path','mime','width','height','bytes')))
                    plan.append(base | {'asset_id':aid,'delivery_asset_id':delivery_id,
                        'crop':True,'box':region['box'],'view_revision':view.get('revision')})
            else:
                plan.append(base | {'asset_id':aid,'view_revision':view.get('revision')})
    return plan


def delivery_bytes(entry):
    item = cache_store.get_item(entry['item_key'])
    if not item or item.get('source_version') != entry['source_version']:
        raise ValueError('图片正文版本或账号已变化')
    # Compare the complete selection, including view revision. An old original
    # manifest must not bypass a newly saved crop (or vice versa).
    if entry not in collect([item]):
        raise ValueError('裁剪选择已变化，请重新预览')
    if not entry.get('crop'):
        return original_bytes(entry)
    record = cache_store.asset_record(entry['delivery_asset_id'])
    if not record:
        raise ValueError('裁剪图缓存缺失')
    path = Path(record['path']).resolve()
    path.relative_to(Path(cache_store.cache_settings()['assets_dir']).resolve())
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('裁剪图超过企业微信图片大小限制')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['delivery_asset_id']:
        raise ValueError('裁剪图校验失败')
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in ('PNG','JPEG'):
            raise ValueError('裁剪图格式不支持')
        image.verify()
    return raw


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


def bundle_key(text, images, *, rendered=None):
    # Validation revisions must not turn an identical saved image into a new
    # delivery. Receipts follow actual ordered bytes and visible captions.
    visible = [{k:e.get(k) for k in ('item_key','course','title','deadline')}
               | {'image':e.get('delivery_asset_id') or e['asset_id'],
                  'crop':bool(e.get('crop'))} for e in images]
    payload = [text, visible] if rendered is None else [text, visible, rendered]
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
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
    # Crops have no anonymous platform URL. Use the existing local byte upload
    # channel for the entire bundle; never substitute an original URL.
    if any(e.get('crop') for e in images):
        return None
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    marker = r'^   作业原图随提醒发送（\d+ 张）。$'
    if len(re.findall(marker, text, re.M)) != len({e['item_key'] for e in images}):
        return None
    groups = {}
    try:
        for entry in images:
            raw = delivery_bytes(entry)
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
        f'![作业原图]({url})' for entry, url in next(cursor)) + '\n\n', body, flags=re.M)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    # Keep text compact with Markdown hard line breaks. Only images get new
    # paragraph boundaries; retain the source's original section spacing.
    body = re.sub(r'(?<!\n)\n(?!\n)', '  \n', body)
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
