# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import io
import struct
import zlib
from pathlib import Path
from unittest.mock import patch

from helpers import FakeContext, FakePage, IsolatedCase

import content_pipeline as cp
import scrape


def make_png(rgb=(255, 0, 0)) -> bytes:
    def chunk(name, data):
        return (struct.pack(">I", len(data)) + name + data
                + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes(rgb)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeStreamResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body
        self.pos = 0
        self.read_sizes = []
        self.closed = False

    def read(self, size):
        self.read_sizes.append(size)
        block = self.body[self.pos:self.pos + size]
        self.pos += len(block)
        return block

    def close(self):
        self.closed = True


class FakeStreamSession:
    def __init__(self, responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.urls = []
        self.responses_seen = []

    def open_stream(self, url):
        self.urls.append(url)
        response = self.responses[url].pop(0)
        self.responses_seen.append(response)
        return response


class StructuredParserTests(IsolatedCase):
    def test_ordered_values_nested_lists_and_table_order(self):
        html = (
            '<p>题目 <span class="math">x^2</span></p>'
            '<ol start="5"><li value="9">第一 <strong>问</strong>'
            '<ol start="2"><li>子题</li></ol></li><li>第二问</li></ol>'
            '<table><tr><th>列</th><th>说明</th></tr>'
            '<tr><td>前<img src="https://course.buct.edu.cn/a.png">后</td>'
            '<td><a href="/f.pdf">附件</a></td></tr></table>'
        )
        blocks = cp.parse_blocks(html, "https://course.buct.edu.cn/meol/x")
        self.assertEqual(blocks[0]["type"], "paragraph")
        self.assertIn("x^2", blocks[0]["text"])
        ordered = next(b for b in blocks if b["type"] == "list")
        self.assertEqual(ordered["start"], 5)
        self.assertEqual([x["value"] for x in ordered["items"]], [9, 10])
        nested = next(x for x in ordered["items"][0]["blocks"] if x["type"] == "list")
        self.assertEqual(nested["start"], 2)
        table = next(b for b in blocks if b["type"] == "table")
        self.assertEqual(table["rows"][1], ["前后", "附件"])
        cell = table["cells"][1][0]
        kinds = [s["type"] for s in cell["blocks"][0]["segments"]]
        self.assertEqual(kinds, ["text", "image", "text"])
        self.assertEqual(table["cells"][1][1]["blocks"][0]["type"], "attachment")

    def test_character_references_are_decoded_once(self):
        blocks = cp.parse_blocks("<p>&amp;lt; &amp;amp;</p>", "https://course.buct.edu.cn/")
        self.assertEqual(blocks[0]["text"], "&lt; &amp;")

    def test_hidden_field_value_is_decoded_once(self):
        raw = '<input type="hidden" name="12_content" value="&lt;p&gt;A &amp;amp; B&lt;/p&gt;">'
        inner = cp.extract_hidden_content(raw)
        self.assertEqual(inner, "<p>A &amp; B</p>")
        self.assertEqual(cp.flatten_blocks_text(cp.parse_blocks(inner, "https://course.buct.edu.cn/")),
                         "A & B")

    def test_images_inside_table_cells_are_found_for_download(self):
        html = ('<table><tr><td>前<img src="https://course.buct.edu.cn/a.png">后</td>'
                '<td><a href="/f.docx">附件</a></td></tr></table>')
        blocks = cp.parse_blocks(html, "https://course.buct.edu.cn/")
        images = list(cp._iter_images(blocks))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["source_url"], "https://course.buct.edu.cn/a.png")


class ImageDownloadTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.write_config({"content": {"asset_max_bytes": 100, "asset_max_pixels": 100000}})

    def test_stream_limit_is_enforced_before_full_body_is_read(self):
        body = b"x" * 500
        response = FakeStreamResponse(headers={"content-type": "image/png"}, body=body)
        session = FakeStreamSession({"https://course.buct.edu.cn/big.png": [response]})
        with self.assertRaises(cp.AssetError):
            cp.download_image(session, "https://course.buct.edu.cn/big.png")
        self.assertTrue(response.closed)
        self.assertLess(len(response.read_sizes), 3)
        self.assertLessEqual(response.pos, 101)

    def test_redirect_is_checked_per_hop_and_response_is_closed(self):
        first = FakeStreamResponse(status=302,
                                   headers={"location": "https://evil.example/a.png"})
        session = FakeStreamSession({"https://course.buct.edu.cn/r.png": [first]})
        with self.assertRaises(cp.AssetError):
            cp.download_image(session, "https://course.buct.edu.cn/r.png")
        self.assertTrue(first.closed)
        self.assertEqual(len(session.urls), 1)

    def test_non_local_http_is_rejected(self):
        session = FakeStreamSession({})
        with self.assertRaises(cp.AssetError):
            cp.download_image(session, "http://course.buct.edu.cn/a.png")
        self.assertEqual(session.urls, [])

    def test_same_url_new_bytes_changes_asset_id(self):
        red = FakeStreamResponse(headers={"content-type": "image/png"}, body=make_png((255, 0, 0)))
        blue = FakeStreamResponse(headers={"content-type": "image/png"}, body=make_png((0, 0, 255)))
        session = FakeStreamSession({"https://course.buct.edu.cn/a.png": [red, blue]})
        one = cp.download_image(session, "https://course.buct.edu.cn/a.png")
        two = cp.download_image(session, "https://course.buct.edu.cn/a.png")
        self.assertNotEqual(one["asset_id"], two["asset_id"])
        self.assertEqual(one["asset_id"], hashlib.sha256(Path(one["path"]).read_bytes()).hexdigest())

    def test_ingest_downloads_images_embedded_in_table_cells(self):
        response = FakeStreamResponse(headers={"content-type": "image/png"}, body=make_png())
        session = FakeStreamSession({"https://course.buct.edu.cn/a.png": [response]})
        result = cp.ingest_html(
            '<table><tr><td>前<img src="/a.png">后</td></tr></table>',
            "https://course.buct.edu.cn/meol/x", session=session)
        table = result["blocks"][0]
        seg = table["cells"][0][0]["blocks"][0]["segments"][1]
        self.assertEqual(seg["type"], "image")
        self.assertEqual(seg["status"], "ready")
        self.assertIn(seg["asset_id"], result["asset_ids"])

    def test_without_pillow_raw_bytes_are_kept_and_crop_is_disabled(self):
        data = make_png()
        response = FakeStreamResponse(headers={"content-type": "image/png"}, body=data)
        session = FakeStreamSession({"https://course.buct.edu.cn/a.png": [response]})
        with patch.object(cp, "_load_pillow", return_value=None):
            asset = cp.download_image(session, "https://course.buct.edu.cn/a.png")
        self.assertFalse(asset["crop_safe"])
        self.assertFalse(asset["orientation_corrected"])
        self.assertEqual(asset["asset_id"], hashlib.sha256(data).hexdigest())
        self.assertEqual(Path(asset["original_path"]).read_bytes(), data)
        self.assertEqual(asset["path"], asset["original_path"])

    def test_exif_orientation_derives_display_but_preserves_source_bytes(self):
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001
            self.skipTest("Pillow not installed")
        self.write_config({"content": {"asset_max_bytes": 10000,
                                       "asset_max_pixels": 100000}})
        image = Image.new("RGB", (2, 3), (255, 0, 0))
        exif = Image.Exif()
        exif[274] = 6
        out = io.BytesIO()
        image.save(out, format="JPEG", exif=exif)
        data = out.getvalue()
        response = FakeStreamResponse(headers={"content-type": "image/jpeg"}, body=data)
        session = FakeStreamSession({"https://course.buct.edu.cn/r.jpg": [response]})
        asset = cp.download_image(session, "https://course.buct.edu.cn/r.jpg")
        self.assertTrue(asset["crop_safe"])
        self.assertTrue(asset["orientation_corrected"])
        self.assertEqual(asset["asset_id"], hashlib.sha256(data).hexdigest())
        self.assertEqual(Path(asset["original_path"]).read_bytes(), data)
        self.assertNotEqual(asset["path"], asset["original_path"])
        self.assertEqual((asset["width"], asset["height"]), (3, 2))

    def test_source_byte_change_changes_ingest_version_even_if_asset_looks_same(self):
        one = make_png((255, 0, 0))
        two = make_png((254, 0, 0))
        versions = []
        for body in (one, two):
            response = FakeStreamResponse(headers={"content-type": "image/png"}, body=body)
            session = FakeStreamSession({"https://course.buct.edu.cn/a.png": [response]})
            with patch.object(cp, "_load_pillow", return_value=None):
                result = cp.ingest_html('<p>X<img src="/a.png"></p>',
                                        "https://course.buct.edu.cn/meol/x",
                                        session=session)
            versions.append(result["source_version"])
        self.assertNotEqual(versions[0], versions[1])


class DetailCourseContextTests(IsolatedCase):
    def test_detail_navigates_to_owning_course_first_and_uses_hidden_parser(self):
        page = FakePage(
            text="课程作业",
            content_html='<input type="hidden" name="1_content" value="&lt;p&gt;A &amp;amp; B&lt;/p&gt;">')
        result = scrape.get_hw_content(FakeContext(page), "88", lid="11", course_name="高数")
        self.assertTrue(result["ok"], result)
        self.assertIn("enter_course.jsp?lid=11", page.gotos[0])
        self.assertIn("hwtask.view.jsp?hwtid=88", page.gotos[1])
        self.assertEqual(result["content_text"], "A & B")

    def test_detail_without_course_lid_is_rejected(self):
        page = FakePage(text="课程作业")
        result = scrape.get_hw_content(FakeContext(page), "88")
        self.assertFalse(result["ok"])
        self.assertEqual(page.gotos, [])


