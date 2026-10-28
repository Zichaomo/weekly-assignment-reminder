# -*- coding: utf-8 -*-
"""Structured, safe conversion of homework HTML and cached raster images.

The parser never trusts the platform DOM for execution.  It builds a small
tree with ``html.parser`` (which decodes character references exactly once),
then emits ordered blocks/segments.  Raw HTML stays on the server.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import mimetypes
import os
import re
import ssl
import struct
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import cache_store

ROOT = Path(__file__).resolve().parent
DEFAULT_ALLOWED_HOSTS = ("course.buct.edu.cn", "*.buct.edu.cn")
ATTACHMENT_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".txt", ".csv", ".md",
}
IMAGE_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp",
}
IGNORE_TAGS = {"script", "style", "template", "noscript", "iframe", "object",
               "embed", "form", "input", "button", "select", "textarea"}
BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
              "ul", "ol", "table", "hr"}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "source", "wbr"}


class HiddenContentParser(HTMLParser):
    """Read the platform's numeric hidden field without decoding it twice."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.value = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input" or self.value is not None:
            return
        attrs = {str(k).lower(): (v or "") for k, v in attrs}
        if re.fullmatch(r"\d+_content", attrs.get("name", "")):
            self.value = attrs.get("value", "")


def extract_hidden_content(raw_html: str) -> str | None:
    parser = HiddenContentParser()
    parser.feed(raw_html or "")
    parser.close()
    return parser.value


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[_Node | str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        node = _Node(tag, {str(k).lower(): (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def _clean_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path,
                       parts.query, ""))


def safe_url(base_url: str, value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return _clean_url(urljoin(base_url, value))
    except ValueError:
        return None


def host_allowed(url: str, allowed_hosts=None) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    for rule in allowed_hosts or DEFAULT_ALLOWED_HOSTS:
        rule = str(rule).lower().strip().rstrip(".")
        if rule.startswith("*."):
            suffix = rule[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == rule:
            return True
    return False


def _is_attachment(href: str) -> bool:
    suffix = Path(urlsplit(href or "").path).suffix.lower()
    low = (href or "").lower()
    return suffix in ATTACHMENT_EXT or "attach" in low or "download" in low


def _norm_text(value: str, *, preserve_space: bool = False) -> str:
    value = (value or "").replace("\u00a0", " ")
    if preserve_space:
        return value
    return " ".join(value.split())


class _Converter:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.block_id = 0

    def _next_id(self, prefix: str) -> str:
        self.block_id += 1
        return f"{prefix}{self.block_id}"

    def image_segment(self, node: _Node) -> dict | None:
        src = (node.attrs.get("data-src") or node.attrs.get("data-original")
               or node.attrs.get("src") or "")
        resolved = safe_url(self.base_url, src)
        if not resolved:
            return None
        return {"type": "image", "source_url": resolved,
                "alt": node.attrs.get("alt", ""), "asset_id": None,
                "status": "pending"}

    def link_segment(self, node: _Node, text: str) -> dict | None:
        href = safe_url(self.base_url, node.attrs.get("href", ""))
        if not href:
            return None
        if _is_attachment(href):
            return {"type": "attachment", "url": href,
                    "text": _norm_text(text) or Path(urlsplit(href).path).name}
        return {"type": "link", "url": href, "text": _norm_text(text)}

    def inline(self, node: _Node, *, preserve_space: bool = False) -> list[dict]:
        out: list[dict] = []

        def text_seg(value: str):
            value = _norm_text(value, preserve_space=preserve_space)
            if not value:
                return
            if out and out[-1].get("type") == "text":
                out[-1]["text"] += value
            else:
                out.append({"type": "text", "text": value})

        def walk(item: _Node | str):
            if isinstance(item, str):
                text_seg(item)
                return
            tag = item.tag
            if tag in IGNORE_TAGS:
                return
            if tag == "br":
                text_seg("\n")
                return
            if tag == "img":
                seg = self.image_segment(item)
                if seg:
                    out.append(seg)
                return
            if tag == "a":
                child_inline = []
                for child in item.children:
                    if isinstance(child, str):
                        child_inline.append(child)
                    elif child.tag == "img":
                        seg = self.image_segment(child)
                        if seg:
                            out.append(seg)
                    elif child.tag not in IGNORE_TAGS:
                        child_inline.extend(self.inline(child))
                text = "".join(x for x in child_inline if isinstance(x, str))
                seg = self.link_segment(item, text)
                if seg:
                    out.append(seg)
                return
            for child in item.children:
                walk(child)

        for child in node.children:
            walk(child)
        return out

    def _segments_have_content(self, segments: list[dict]) -> bool:
        return any(s.get("text") or s.get("asset_id") or s.get("source_url")
                   or s.get("url") for s in segments)

    def _segments_text(self, segments: list[dict]) -> str:
        parts = []
        for seg in segments:
            if seg.get("type") == "text":
                parts.append(seg.get("text", ""))
            elif seg.get("type") == "link":
                parts.append(seg.get("text", "") or seg.get("url", ""))
            elif seg.get("type") == "attachment":
                parts.append(seg.get("text", ""))
        return "".join(parts).strip()

    def paragraph(self, segments: list[dict], *, pre: bool = False) -> dict | None:
        if not self._segments_have_content(segments):
            return None
        return {"id": self._next_id("p"), "type": "paragraph",
                "segments": segments, "text": self._segments_text(segments),
                "pre": bool(pre)}

    def _has_block_child(self, node: _Node) -> bool:
        return any(isinstance(ch, _Node) and ch.tag in BLOCK_TAGS
                   for ch in node.children)

    def mixed(self, node: _Node, *, preserve_space: bool = False) -> list[dict]:
        blocks: list[dict] = []
        inline_buf: list[_Node | str] = []

        def flush():
            nonlocal inline_buf
            if not inline_buf:
                return
            holder = _Node("#inline")
            holder.children = inline_buf
            segs = self.inline(holder, preserve_space=preserve_space)
            para = self.paragraph(segs, pre=preserve_space)
            if para:
                blocks.append(para)
            inline_buf = []

        for child in node.children:
            if isinstance(child, str):
                inline_buf.append(child)
                continue
            tag = child.tag
            if tag in IGNORE_TAGS:
                continue
            if tag in BLOCK_TAGS:
                flush()
                if tag in ("ul", "ol"):
                    blocks.append(self.list_block(child))
                elif tag == "table":
                    blocks.append(self.table_block(child))
                elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6",
                              "blockquote", "pre"):
                    para = self.paragraph(
                        self.inline(child, preserve_space=(tag == "pre")),
                        pre=(tag == "pre"))
                    if para:
                        blocks.append(para)
                elif tag == "hr":
                    blocks.append({"id": self._next_id("h"), "type": "divider"})
                elif self._has_block_child(child):
                    blocks.extend(self.mixed(child, preserve_space=preserve_space))
                else:
                    para = self.paragraph(self.inline(child))
                    if para:
                        blocks.append(para)
                continue
            if tag == "a":
                href = safe_url(self.base_url, child.attrs.get("href", ""))
                if href and _is_attachment(href):
                    flush()
                    blocks.append(self.attachment_block(child, href))
                    continue
            inline_buf.append(child)
        flush()
        return [b for b in blocks if b]

    def attachment_block(self, node: _Node, href: str | None = None) -> dict:
        href = href or safe_url(self.base_url, node.attrs.get("href", "")) or ""
        text = _norm_text("".join(
            x.get("text", "") for x in self.inline(node)
            if x.get("type") == "text" and x.get("text")))
        return {"id": self._next_id("a"), "type": "attachment",
                "text": text or Path(urlsplit(href).path).name, "url": href}

    def list_block(self, node: _Node) -> dict:
        ordered = node.tag == "ol"
        try:
            counter = int(str(node.attrs.get("start", "1")).strip())
        except ValueError:
            counter = 1
        declared_start = counter
        items = []
        for child in node.children:
            if not isinstance(child, _Node) or child.tag != "li":
                continue
            value = counter
            if child.attrs.get("value"):
                try:
                    value = int(str(child.attrs["value"]).strip())
                except ValueError:
                    pass
            item_blocks = self.mixed(child)
            item_text = "\n".join(self.block_text(b) for b in item_blocks if self.block_text(b))
            items.append({"value": value, "blocks": item_blocks, "text": item_text})
            counter = value + 1
        return {"id": self._next_id("l"), "type": "list",
                "ordered": ordered, "start": declared_start if ordered else None,
                "items": items}

    def table_block(self, node: _Node) -> dict:
        rows: list[list[str]] = []
        structured: list[list[dict]] = []

        def visit_table(table: _Node):
            for child in table.children:
                if not isinstance(child, _Node):
                    continue
                if child.tag == "tr":
                    row_text, row_cells = [], []
                    for cell in child.children:
                        if not isinstance(cell, _Node) or cell.tag not in ("td", "th"):
                            continue
                        cell_blocks = self.mixed(cell)
                        cell_text = "\n".join(
                            self.block_text(b) for b in cell_blocks if self.block_text(b))
                        row_text.append(cell_text)
                        row_cells.append({"text": cell_text, "blocks": cell_blocks,
                                          "header": cell.tag == "th"})
                    if row_cells:
                        rows.append(row_text)
                        structured.append(row_cells)
                elif child.tag in ("thead", "tbody", "tfoot"):
                    visit_table(child)

        visit_table(node)
        return {"id": self._next_id("t"), "type": "table",
                "rows": rows, "cells": structured}

    def block_text(self, block: dict) -> str:
        typ = block.get("type")
        if typ in ("paragraph",):
            return str(block.get("text") or "")
        if typ == "attachment":
            return str(block.get("text") or "")
        if typ == "list":
            lines = []
            for item in block.get("items") or []:
                prefix = f"{item.get('value')}. " if block.get("ordered") else "• "
                body = "\n".join(self.block_text(b) for b in item.get("blocks") or []
                                 if self.block_text(b))
                lines.append(prefix + body)
            return "\n".join(lines)
        if typ == "table":
            return "\n".join(" | ".join(row) for row in block.get("rows") or [])
        return ""

    def blocks(self, root: _Node) -> list[dict]:
        return self.mixed(root)


def parse_blocks(raw_html: str, base_url: str) -> list[dict]:
    builder = _TreeBuilder()
    builder.feed(raw_html or "")
    builder.close()
    return _Converter(base_url).blocks(builder.root)


def _iter_images(items):
    for block in items or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            yield block
        for seg in block.get("segments") or []:
            if isinstance(seg, dict) and seg.get("type") == "image":
                yield seg
        for item in block.get("items") or []:
            yield from _iter_images(item.get("blocks") or [])
        for row in block.get("cells") or []:
            for cell in row:
                yield from _iter_images(cell.get("blocks") or [])


def flatten_blocks_text(blocks: list[dict]) -> str:
    converter = _Converter("")
    return "\n".join(x for x in (converter.block_text(b) for b in blocks or []) if x)


def sniff_image(data: bytes) -> tuple[str | None, int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", w, h
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return "image/gif", w, h
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if i + 2 > len(data):
                break
            length = struct.unpack(">H", data[i:i + 2])[0]
            if length < 2 or i + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 3:i + 7])
                return "image/jpeg", w, h
            i += length
        return "image/jpeg", None, None
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return "image/webp", w, h
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", data[26:30])
            return "image/webp", w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return "image/webp", None, None
    return None, None, None


def _load_pillow():
    try:
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001
        return None
    return Image, ImageOps


def _decode_image(data: bytes, max_pixels: int):
    """Validate with Pillow; return display bytes only when re-encoding is needed.

    Without Pillow the raw image remains usable for viewing, but cropping is
    explicitly unsafe (orientation/decoding could not be verified).
    """
    mime, width, height = sniff_image(data)
    if not mime or not width or not height:
        raise AssetError("图片格式无法识别或尺寸无效")
    if width * height > max_pixels:
        raise AssetError("图片像素超过限制")
    pillow = _load_pillow()
    if not pillow:
        return {"mime": mime, "source_mime": mime,
                "width": int(width), "height": int(height),
                "display": None, "orientation": None, "decoded": False,
                "crop_safe": False, "orientation_corrected": False}
    Image, ImageOps = pillow
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width * image.height > max_pixels:
                raise AssetError("图片像素超过限制")
            try:
                orientation = int(image.getexif().get(274) or 1)
            except Exception:  # noqa: BLE001
                orientation = 1
            corrected = ImageOps.exif_transpose(image)
            out_w, out_h = int(corrected.width), int(corrected.height)
            # Keep the exact original bytes whenever no orientation transform
            # is needed.  This prevents a derived PNG from replacing the source.
            if orientation in (0, 1) and corrected.size == image.size:
                return {"mime": mime, "source_mime": mime,
                        "width": out_w, "height": out_h,
                        "display": None, "orientation": orientation,
                        "decoded": True, "crop_safe": True,
                        "orientation_corrected": False}
            if corrected.mode not in ("RGB", "RGBA"):
                corrected = corrected.convert(
                    "RGBA" if "A" in corrected.getbands() else "RGB")
            out = io.BytesIO()
            corrected.save(out, format="PNG", optimize=False)
            return {"mime": "image/png", "source_mime": mime,
                    "width": int(corrected.width),
                    "height": int(corrected.height), "display": out.getvalue(),
                    "orientation": orientation, "decoded": True,
                    "crop_safe": True, "orientation_corrected": True}
    except AssetError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AssetError(f"图片解码失败：{type(exc).__name__}") from exc


def _validate_fetch_url(url: str, allowed_hosts) -> None:
    parts = urlsplit(url)
    if parts.username or parts.password or not parts.hostname:
        raise AssetError("图片地址包含凭据或缺少主机")
    if not host_allowed(url, allowed_hosts):
        raise AssetError("图片地址不在可信域")
    local = parts.hostname.lower() in ("127.0.0.1", "localhost", "::1")
    if parts.scheme.lower() != "https" and not (local and parts.scheme.lower() == "http"):
        raise AssetError("图片下载只允许 HTTPS（本机测试地址除外）")


class AssetError(RuntimeError):
    pass


class _HttpStream:
    def __init__(self, response, connection):
        self.response = response
        self.connection = connection
        self.status = int(response.status)
        self.headers = {str(k).lower(): str(v) for k, v in (response.getheaders() or [])}

    def read(self, size: int) -> bytes:
        return self.response.read(size)

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            try:
                self.connection.close()
            except Exception:  # noqa: BLE001
                pass


class PlaywrightAssetSession:
    """Stream over HTTP(S), reusing only the current Playwright context cookies."""

    def __init__(self, ctx, timeout_ms: int = 20000):
        self.ctx = ctx
        self.timeout_ms = timeout_ms

    def _cookie_header(self, url: str) -> str:
        try:
            cookies = self.ctx.cookies([url]) or []
        except Exception:  # noqa: BLE001
            cookies = []
        return "; ".join(f"{c.get('name')}={c.get('value')}" for c in cookies
                         if c.get("name"))

    def open_stream(self, url: str):
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port, timeout=max(1.0, self.timeout_ms / 1000),
                context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(
                host, port, timeout=max(1.0, self.timeout_ms / 1000))
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        headers = {"Accept": "image/*", "User-Agent": "homework-console/1"}
        cookie = self._cookie_header(url)
        if cookie:
            headers["Cookie"] = cookie
        try:
            conn.request("GET", path, headers=headers)
            return _HttpStream(conn.getresponse(), conn)
        except Exception:
            conn.close()
            raise

    def fetch(self, url: str) -> dict:
        response = self.ctx.request.get(url, max_redirects=0, timeout=self.timeout_ms)
        headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
        return {"status": response.status, "headers": headers, "body": response.body()}


def _read_limited(response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise AssetError("图片超过大小限制")
        except ValueError:
            pass
    chunks = []
    total = 0
    while True:
        block = response.read(min(64 * 1024, max_bytes + 1 - total))
        if not block:
            break
        total += len(block)
        if total > max_bytes:
            raise AssetError("图片超过大小限制")
        chunks.append(block)
    return b"".join(chunks)


def _fetch_follow(session, url: str, *, allowed_hosts, max_bytes: int,
                  max_redirects: int = 3):
    current = url
    seen = set()
    for _ in range(max_redirects + 1):
        if current in seen:
            raise AssetError("图片重定向循环")
        seen.add(current)
        _validate_fetch_url(current, allowed_hosts)
        response = None
        if hasattr(session, "open_stream"):
            response = session.open_stream(current)
            try:
                status = int(response.status)
                headers = response.headers
                if status in (301, 302, 303, 307, 308):
                    location = headers.get("location")
                    if not location:
                        raise AssetError("图片重定向缺少 Location")
                    nxt = safe_url(current, location)
                    if not nxt:
                        raise AssetError("图片重定向地址非法")
                    current = nxt
                    continue
                if not 200 <= status < 300:
                    raise AssetError(f"图片下载失败 HTTP {status}")
                return current, headers, _read_limited(response, max_bytes)
            finally:
                response.close()
        result = session.fetch(current)
        status = int(result.get("status") or 0)
        headers = {str(k).lower(): str(v) for k, v in (result.get("headers") or {}).items()}
        if status in (301, 302, 303, 307, 308):
            nxt = safe_url(current, headers.get("location") or "")
            if not nxt:
                raise AssetError("图片重定向地址非法")
            current = nxt
            continue
        if not 200 <= status < 300:
            raise AssetError(f"图片下载失败 HTTP {status}")
        data = bytes(result.get("body") or b"")
        if len(data) > max_bytes:
            raise AssetError("图片超过大小限制")
        return current, headers, data
    raise AssetError("图片重定向过多")


def _atomic_write_asset(data: bytes, asset_id: str, suffix: str) -> Path:
    settings = cache_store.cache_settings()
    folder = Path(settings["assets_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{asset_id}{suffix}"
    if target.exists() and target.stat().st_size == len(data):
        try:
            if hashlib.sha256(target.read_bytes()).hexdigest() == asset_id:
                return target
        except OSError:
            pass
    fd, tmp_name = tempfile.mkstemp(prefix=".asset-", dir=str(folder))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
    return target


def download_image(session, url: str, *, allowed_hosts=None) -> dict:
    if not session:
        raise AssetError("没有可用于下载图片的平台会话")
    settings = cache_store.cache_settings()
    max_bytes = int(settings["asset_max_bytes"])
    max_pixels = int(settings["asset_max_pixels"])
    final_url, headers, data = _fetch_follow(
        session, url, allowed_hosts=allowed_hosts or DEFAULT_ALLOWED_HOSTS,
        max_bytes=max_bytes)
    if not data:
        raise AssetError("图片为空")
    if len(data) > max_bytes:
        raise AssetError("图片超过大小限制")
    info = _decode_image(data, max_pixels)
    declared = (headers.get("content-type") or "").split(";")[0].strip().lower()
    # THEOL's image JSP serves JPEG bytes with this legacy MIME alias.
    declared = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}.get(
        declared, declared)
    if declared.startswith("image/") and declared.split("/")[-1] not in (
            "png", "jpeg", "jpg", "gif", "webp"):
        raise AssetError("图片 MIME 类型不允许")
    if declared.startswith("image/") and declared != info["source_mime"]:
        raise AssetError("图片 MIME 与内容不一致")
    # Asset identity/version is always the *source byte* hash, so replacing an
    # image at the same URL invalidates old crops even when it looks identical.
    digest = hashlib.sha256(data).hexdigest()
    original_path = _atomic_write_asset(data, digest, IMAGE_MIME[info["source_mime"]])
    display = info.get("display")
    if display:
        display_path = _atomic_write_asset(display, digest, ".display.png")
        display_mime = "image/png"
    else:
        display_path = original_path
        display_mime = info["mime"]
    return {"asset_id": digest, "sha256": digest, "path": str(display_path),
            "original_path": str(original_path), "display_path": str(display_path),
            "mime": display_mime, "source_mime": info["source_mime"],
            "width": int(info["width"]), "height": int(info["height"]),
            "bytes": len(data), "source_url": final_url,
            "decoded": bool(info["decoded"]), "crop_safe": bool(info["crop_safe"]),
            "orientation": info.get("orientation"),
            "orientation_corrected": bool(info["orientation_corrected"])}


def ingest_html(raw_html: str, base_url: str, *, session=None,
                allowed_hosts=None) -> dict:
    """Parse decoded homework HTML, downloading only embedded raster images."""
    source_hash = hashlib.sha256((raw_html or "").encode("utf-8")).hexdigest()
    blocks = parse_blocks(raw_html or "", base_url)
    assets: list[dict] = []
    seen_assets = set()
    version_parts = [source_hash]
    for block in _iter_images(blocks):
        url = block.get("source_url")
        try:
            asset = download_image(session, url, allowed_hosts=allowed_hosts)
            block.update({"asset_id": asset["asset_id"], "status": "ready",
                          "width": asset["width"], "height": asset["height"],
                          "mime": asset["mime"], "crop_safe": asset["crop_safe"],
                          "orientation_corrected": asset["orientation_corrected"]})
            if asset["asset_id"] not in seen_assets:
                seen_assets.add(asset["asset_id"])
                assets.append(asset)
                version_parts.append(asset["sha256"])
        except Exception as exc:  # noqa: BLE001
            block.update({"status": "failed", "error": str(exc)[:200]})
            version_parts.append(f"failed:{url}")
    version = hashlib.sha256("|".join(version_parts).encode("utf-8")).hexdigest()[:32]
    return {
        "status": "ready" if blocks else "empty",
        "source_url": base_url,
        "source_hash": source_hash,
        "raw_html": raw_html or "",
        "source_version": version,
        "blocks": blocks,
        "assets": assets,
        "asset_ids": [a["asset_id"] for a in assets],
        "text": flatten_blocks_text(blocks),
    }


def asset_path(asset_id: str) -> tuple[Path, dict] | None:
    record = cache_store.asset_record(asset_id)
    if not record:
        return None
    path = Path(record["path"]).resolve()
    assets_dir = Path(cache_store.cache_settings()["assets_dir"]).resolve()
    try:
        path.relative_to(assets_dir)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path, record
