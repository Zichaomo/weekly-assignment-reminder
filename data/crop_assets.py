"""Deterministic local crop assets. Originals remain immutable."""
import hashlib
import io
import math
from pathlib import Path

import cache_store


def generate(record, box):
    from PIL import Image, ImageOps
    if record.get('crop_safe') == 0:
        raise ValueError('该原图尚未通过方向与解码校验，请刷新正文后再裁剪')
    root = Path(cache_store.cache_settings()['assets_dir']).resolve()
    source = Path(record.get('original_path') or record['path']).resolve()
    source.relative_to(root)
    if source.stat().st_size > cache_store.cache_settings()['asset_max_bytes']:
        raise ValueError('原图超过本地图片大小限制')
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record['asset_id']:
        raise ValueError('原图校验失败，无法保存裁剪')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > cache_store.cache_settings()['asset_max_pixels']:
            raise ValueError('图片像素超过限制')
        image = ImageOps.exif_transpose(image)
        w, h = image.size
        x1,y1,x2,y2 = box
        bounds = (math.floor(x1*w), math.floor(y1*h), math.ceil(x2*w), math.ceil(y2*h))
        cropped = image.crop(bounds).convert('RGBA' if 'A' in image.getbands() else 'RGB')
        out = io.BytesIO()
        cropped.save(out, format='PNG', optimize=False)
        data = out.getvalue()
        width, height = cropped.size
    if len(data) > 2 * 1024 * 1024:
        raise ValueError('裁剪图超过企业微信 2 MB 图片限制，请缩小选区')
    digest = hashlib.sha256(data).hexdigest()
    path = root / (digest + '.png')
    # Content-addressed paths are safe under concurrent saves of identical bytes.
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        import os
        import tempfile
        fd, name = tempfile.mkstemp(prefix='crop-', dir=root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return {'asset_id':digest,'sha256':digest,'path':str(path),
            'mime':'image/png','width':width,'height':height,'bytes':len(data)}
