"""Account-bound additions supplied independently of the platform body."""
import base64
import binascii
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import cache_store
import content_pipeline

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TEXT = 4000


def load(con, item_key, generation):
    row = con.execute('SELECT * FROM assignment_supplements WHERE item_key=?',
                      (item_key,)).fetchone()
    current = row and row['identity_generation'] == generation
    return {'revision': str(row['revision']) if row else 'none',
            'identity_generation': generation,
            'text': row['text'] if current else '',
            'images': json.loads(row['images_json']) if current else [],
            'updated_at': cache_store._iso(row['updated_at']) if current else None}


def decode_upload(encoded):
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError('每张补充图片最多 2 MB')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('补充图片数据无效') from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('每张补充图片最多 2 MB')
    try:
        decoded = content_pipeline._decode_image(raw, cache_store.cache_settings()['asset_max_pixels'])
    except content_pipeline.AssetError as exc:
        raise ValueError(str(exc)) from exc
    if not decoded['decoded']:
        raise ValueError('请安装 Pillow 后上传补充图片')
    if decoded['source_mime'] not in ('image/png', 'image/jpeg'):
        raise ValueError('补充图片支持 PNG 和 JPG')
    raw = decoded.get('display') or raw
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('方向校正后的图片超过 2 MB，请缩小后上传')
    return raw, {'asset_id': hashlib.sha256(raw).hexdigest(),
                 'mime': decoded['mime'], 'width': decoded['width'], 'height': decoded['height']}


def save(item_key, payload):
    text = payload.get('text', '')
    images = payload.get('images', [])
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('补充文字最多 4000 字')
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError('最多添加 6 张补充图片')
    cache_store.init_db()
    with cache_store.connection(write=True) as con:
        item = con.execute('SELECT * FROM assignments WHERE item_key=? AND account_scope=? AND platform=?',
                           (item_key, cache_store.account_scope(), cache_store.PLATFORM)).fetchone()
        if not item or item['kind'] != 'homework':
            raise ValueError('请选择平台作业补充要求')
        generation = cache_store._scope_identity_generation(item['account_scope'], con)
        current = load(con, item_key, generation)
        if (payload.get('expected_revision') != current['revision']
                or payload.get('expected_identity_generation') != generation):
            raise cache_store.VersionConflict('补充内容或账号已变化，请重新打开并核对草稿')
        existing = {image['asset_id']: image for image in current['images']}
        selected, uploads = [], {}
        for image in images:
            if not isinstance(image, dict):
                raise ValueError('补充图片格式错误')
            if 'data' in image:
                raw, metadata = decode_upload(image['data'])
                uploads[metadata['asset_id']] = (raw, metadata)
            else:
                aid = image.get('asset_id')
                if not isinstance(aid, str):
                    raise ValueError('补充图片 ID 格式错误')
                metadata = existing.get(aid)
                if not metadata:
                    raise ValueError('图片不属于本条作业的补充内容，请重新上传')
            if metadata not in selected:
                selected.append(metadata)
        directory = Path(cache_store.cache_settings()['assets_dir'])
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for aid, (raw, metadata) in uploads.items():
            suffix = '.png' if metadata['mime'] == 'image/png' else '.jpg'
            path = directory / (aid + suffix)
            descriptor, temporary_name = tempfile.mkstemp(dir=directory)
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, 'wb') as pending:
                    pending.write(raw)
                os.replace(temporary_path, path)
            finally:
                temporary_path.unlink(missing_ok=True)
            con.execute('''INSERT OR IGNORE INTO assets
                (asset_id,sha256,path,mime,width,height,bytes,checked_at,original_path,display_path)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (aid, aid, str(path), metadata['mime'], metadata['width'], metadata['height'],
                 len(raw), now, str(path), str(path)))
        revision = 1 if current['revision'] == 'none' else int(current['revision']) + 1
        con.execute('''INSERT OR REPLACE INTO assignment_supplements
            (item_key,identity_generation,text,images_json,revision,updated_at) VALUES(?,?,?,?,?,?)''',
            (item_key, generation, text.strip(), json.dumps(selected), revision, now))
        return load(con, item_key, generation)
