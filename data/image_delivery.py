"""Version-bound original/crop plans shared by console and scheduled publishing."""
import hashlib
import io
import json
import re
import urllib.request
from urllib.parse import quote
from pathlib import Path

import cache_store
import content_summary

# Ideographic spaces remain visible in Markdown and ordinary text messages.
DETAIL_INDENT = '\u3000\u3000'
IMAGE_MARKER = re.compile(r'^(?: {3}|\u3000{2})(作业原图|作业图片|补充图片)随提醒发送（(\d+) 张）。$', re.M)


def collect(items):
    plan = []
    for item in items:
        if item.get('kind') in ('test', 'manual') or item.get('source') == 'manual':
            continue
        supplement = item.get('supplement') or {}
        supplement_plan = []
        for image in supplement.get('images', []):
            supplement_plan.append({k: item.get(k) for k in ('item_key', 'course', 'title', 'deadline')}
                        | {'asset_id': image['asset_id'], 'supplement': True,
                           'supplement_revision': supplement['revision'],
                           'identity_generation': supplement['identity_generation']})
        summary = content_summary.summarize(item)
        if summary['status'] != 'images':
            plan.extend(supplement_plan)
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
        plan.extend(supplement_plan)
    return plan


def delivery_bytes(entry):
    if entry.get('supplement'):
        return original_bytes(entry)
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
    if not item:
        raise ValueError('图片所属作业或账号已变化')
    if entry.get('supplement'):
        if entry not in collect([item]):
            raise ValueError('补充内容或账号已变化，请重新预览')
    elif item.get('source_version') != entry['source_version']:
        raise ValueError('图片正文版本已变化')
    summary = content_summary.summarize(item)
    if not entry.get('supplement') and (summary['status'] != 'images' or entry['asset_id'] not in summary['asset_ids']):
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


def message_parts(text, images):
    """Replace each image marker with its selected images in message order."""
    groups = {}
    for entry in images:
        groups.setdefault((entry['item_key'], bool(entry.get('supplement'))), []).append(entry)
    parts, pending, used = [], [], set()
    heading = ''
    for line in text.replace('\r\n', '\n').replace('\r', '\n').splitlines(keepends=True):
        content = line.rstrip('\n')
        if content.startswith('• '):
            heading = content
        match = IMAGE_MARKER.fullmatch(content)
        if not match:
            pending.append(line)
            continue
        candidates = []
        for key, entries in groups.items():
            first = entries[0]
            if key in used or key[1] != (match[1] == '补充图片'):
                continue
            title = str(first.get('title') or '')
            course = first.get('course')
            matches = (heading in (f'• {course}｜{title}', f'• {course} 🎓选修｜{title}')
                       if course else heading.endswith('｜' + title))
            if matches:
                candidates.append(key)
        if len(candidates) != 1:
            raise ValueError('作业图片与正文位置不匹配，请重新预览')
        key = candidates[0]
        if len(groups[key]) != int(match[2]):
            raise ValueError('作业图片数量已变化，请重新预览')
        if pending:
            parts.append({'type': 'text', 'text': ''.join(pending)})
            pending = []
        parts.extend({'type': 'image', 'entry': entry} for entry in groups[key])
        used.add(key)
    if pending:
        parts.append({'type': 'text', 'text': ''.join(pending)})
    if groups and not used:
        # Older custom messages can carry a manifest without image markers.
        for entries in groups.values():
            first = entries[0]
            caption = (f"\n{first.get('course', '')}｜{first.get('title', '')}\n"
                       f"{DETAIL_INDENT}截止：{first.get('deadline') or '未标注'}\n")
            parts.append({'type': 'text', 'text': caption})
            parts.extend({'type': 'image', 'entry': entry} for entry in entries)
        return parts
    if len(used) != len(groups):
        raise ValueError('作业图片缺少对应的正文位置，请重新预览')
    return parts


def inline_markdown(text, images, *, hosting=None):
    """Embed publicly readable URLs matching the exact selected image bytes."""
    if not images:
        return None
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    urls = {}
    import image_hosting
    try:
        parts = message_parts(text, images)
        for entry in images:
            raw = delivery_bytes(entry)
            asset_id = entry.get('delivery_asset_id') or entry['asset_id']
            url = image_hosting.image_url(entry, raw, hosting)
            if not url:
                return None
            urls[asset_id] = quote(url, safe=':/?=&%')
    except image_hosting.HostingError:
        raise
    except (ValueError, OSError):
        return None
    # Escape literal source text before introducing the image Markdown.
    fragments = []
    for part in parts:
        if part['type'] == 'text':
            fragments.append(re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', part['text']))
        else:
            entry = part['entry']
            url = urls[entry.get('delivery_asset_id') or entry['asset_id']]
            fragments.append(f'\n\n![作业原图]({url})\n\n')
    body = ''.join(fragments)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    # Keep text compact with Markdown hard line breaks. Only images get new
    # paragraph boundaries; retain the source's original section spacing.
    body = re.sub(r'(?<!\n)\n(?!\n)', '  \n', body)
    # Keep each homework with its images when measuring the API byte limit.
    # The sender reports when the requested single message exceeds capacity.
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
