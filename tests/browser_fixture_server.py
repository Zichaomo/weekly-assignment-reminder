# -*- coding: utf-8 -*-
"""Local-only HTTP fixture for the browser smoke test.

Seeds an isolated SQLite cache and serves the real console/API over
127.0.0.1.  It never imports platform credentials or opens a school page.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(DATA))


def png_bytes(width=400, height=300, color=(240, 220, 180)):
    def chunk(name, payload):
        return (struct.pack(">I", len(payload)) + name + payload
                + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="homework-browser-smoke-"))
    env = {
        "HOMEWORK_CACHE_DB": str(tmp / "cache.sqlite3"),
        "HOMEWORK_CACHE_ASSETS": str(tmp / "assets"),
        "HOMEWORK_CONFIG_PATH": str(tmp / "config.json"),
        "HOMEWORK_LOCK_PATH": str(tmp / "platform.lock"),
        "HOMEWORK_ACCOUNT_SCOPE": "browser-smoke-isolated",
    }
    os.environ.update(env)
    (tmp / "config.json").write_text("{}", encoding="utf-8")

    import cache_store
    import content_pipeline
    import hub
    import manual
    import scrape
    import server

    # Hard isolation: no real config, platform fetch, browser launch, or send
    # entry point may run inside the fixture.
    cache_store._load_config = lambda: {}
    server.load_channel = lambda: "none"
    server.Handler.LOG_FILE = tmp / "server_error.log"

    class FixtureIsolationError(RuntimeError):
        pass

    def blocked(name):
        def _blocked(*args, **kwargs):
            raise FixtureIsolationError(
                f"{name} is disabled in the browser fixture; "
                "refusing platform/send access")
        return _blocked

    server.submit_task = blocked("server.submit_task")
    server.run_task = blocked("server.run_task")
    hub.run_publish = blocked("hub.run_publish")
    hub.run_content = blocked("hub.run_content")
    hub.run_refresh_courses = blocked("hub.run_refresh_courses")
    if hasattr(os, "startfile"):
        os.startfile = blocked("os.startfile")
    local_run_list = hub.run_list

    def guarded_run_list(days, fetch, *args, **kwargs):
        if fetch:
            raise FixtureIsolationError(
                "hub.run_list(fetch=True) is disabled in the browser fixture")
        return local_run_list(days, False, *args, **kwargs)

    hub.run_list = guarded_run_list

    # Keep every runtime file inside the temporary directory.
    scrape.COURSES_FILE = tmp / "courses.json"
    scrape.BLACKLIST_FILE = tmp / "course_blacklist.json"
    scrape.TEST_BLACKLIST_FILE = tmp / "test_blacklist.json"
    scrape.ELECTIVES_FILE = tmp / "electives.json"
    scrape.META_BACKUP_FILE = tmp / "courses.meta.bak.json"
    scrape.SCAN_SNAPSHOT_FILE = tmp / "courses.scan.json"
    hub.MANUAL_FILE = tmp / "manual.json"
    manual.MANUAL_FILE = tmp / "manual.json"
    hub.MANUAL_FILE.write_text("[]", encoding="utf-8")
    index = {"101": "fixture课程 A"}
    scrape.COURSES_FILE.write_text(json.dumps(
        {"refreshed_at": "fixture", "courses": index, "meta": {}},
        ensure_ascii=False), encoding="utf-8")
    scrape.BLACKLIST_FILE.write_text('{"courses": {}}', encoding="utf-8")
    scrape.TEST_BLACKLIST_FILE.write_text('{"courses": {}}', encoding="utf-8")
    scrape.ELECTIVES_FILE.write_text('{"courses": {}}', encoding="utf-8")

    scope = cache_store.account_scope()
    deadline = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    cache_store.begin_scan("smoke-scan", mode="deadline-only", expected_lids=["101"],
                           requested_lids=["101"], tests_requested=False,
                           course_index_version=cache_store.version_hash(index), scope=scope)
    cache_store.commit_course_scan(
        "smoke-scan", lid="101", course="fixture课程 A",
        homeworks=[{"hwtid": "9001", "title": "fixture作业（脱敏）",
                    "deadline": deadline, "submit_url": None, "result_url": None,
                    "submitted": False}],
        tests=[], homework_status="ok", tests_status="disabled",
        index_version=cache_store.version_hash(index), scope=scope)
    cache_store.update_scan("smoke-scan", status="complete", finished_at=time.time())
    item = cache_store.load_items(scope)[0]
    assets_dir = Path(cache_store.cache_settings()["assets_dir"])
    assets_dir.mkdir(parents=True, exist_ok=True)

    def cache_image(raw, width, height):
        asset_id = hashlib.sha256(raw).hexdigest()
        image_path = assets_dir / f"{asset_id}.png"
        image_path.write_bytes(raw)
        return asset_id, {
            "asset_id": asset_id, "sha256": asset_id,
            "path": str(image_path), "original_path": str(image_path),
            "display_path": str(image_path), "mime": "image/png",
            "width": width, "height": height, "bytes": len(raw),
            "source_url": "https://course.buct.edu.cn/fixture.png",
            "crop_safe": True,
        }

    image = png_bytes(400, 300, (240, 220, 180))
    asset_id, asset = cache_image(image, 400, 300)
    cell_image = png_bytes(480, 360, (205, 225, 240))
    cell_asset_id, cell_asset = cache_image(cell_image, 480, 360)
    blocks = [
        {"id": "p1", "type": "paragraph", "text": "fixture正文：完成第 1、2 题",
         "segments": [{"type": "text", "text": "fixture正文：完成第 1、2 题"}]},
        {"id": "t1", "type": "table", "rows": [["题目", "要求"], ["1", "作图并说明"]],
         "cells": [[{"text": "题目", "header": True, "blocks": []},
                    {"text": "要求", "header": True, "blocks": []}],
                   [{"text": "1", "header": False, "blocks": []},
                    {"text": "", "header": False, "blocks": [
                        {"id": "cell-p1", "type": "paragraph",
                         "text": "作图并说明",
                         "segments": [{"type": "text", "text": "作图并说明"}]},
                        {"id": "cell-p2", "type": "paragraph",
                         "text": "表格内图片：",
                         "segments": [{"type": "text", "text": "表格内图片："}]},
                        {"id": "cell-img", "type": "image",
                         "asset_id": cell_asset_id, "status": "ready",
                         "crop_safe": True, "width": 480, "height": 360,
                         "mime": "image/png", "alt": "表格单元格图片"}]}]]},
        {"id": "p2", "type": "paragraph", "text": "图片：",
         "segments": [{"type": "text", "text": "图片："},
                      {"type": "image", "asset_id": asset_id, "status": "ready",
                       "crop_safe": True, "width": 400, "height": 300,
                       "mime": "image/png", "alt": "正文图片"}]},
    ]
    cache_store.save_content(item["item_key"], {
        "status": "ready", "source_url": "https://course.buct.edu.cn/fixture",
        "source_hash": "fixture-hash", "source_version": "fixture-v1",
        "blocks": blocks, "asset_ids": [asset_id, cell_asset_id],
        "assets": [asset, cell_asset],
        "text": "fixture正文：完成第 1、2 题"}, scope=scope)

    class FixtureHandler(server.Handler):
        def _do_GET(self):
            if urlparse(self.path).path == "/favicon.ico":
                return self._send(204, None, raw=b"")
            return super()._do_GET()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.PORT = httpd.server_port
    print(json.dumps({"port": httpd.server_port, "dir": str(tmp),
                      "item_key": item["item_key"]}), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
