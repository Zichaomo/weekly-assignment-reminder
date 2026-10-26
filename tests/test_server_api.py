# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from helpers import IsolatedCase

import cache_store


class ServerApiTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()
        cache_store.begin_scan("api-scan", mode="test", expected_lids=["1"],
                               requested_lids=["1"], tests_requested=False,
                               course_index_version="idx", scope=self.scope)
        cache_store.commit_course_scan(
            "api-scan", lid="1", course="课程1",
            homeworks=[{"hwtid": "7001", "title": "API 作业",
                        "deadline": "2026-12-31 23:59"}],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version="idx", scope=self.scope)
        cache_store.update_scan("api-scan", status="complete", finished_at=time.time())
        item = cache_store.load_items(self.scope)[0]
        self.key = item["item_key"]
        self.asset_id = "c" * 64
        cache_store.save_content(self.key, {
            "status": "ready", "source_url": "https://course.buct.edu.cn/x",
            "source_hash": "h", "source_version": "v1",
            "blocks": [{"id": "p1", "type": "paragraph", "text": "正文"}],
            "asset_ids": [self.asset_id], "assets": [], "text": "正文"}, scope=self.scope)
        import server
        self.server_mod = server
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(self.base + path, data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def view(self, payload):
        return self.post(f"/api/assignments/{quote(self.key, safe='')}/view", payload)

    def test_content_api_exposes_fields_needed_by_frontend(self):
        status, data = self.get(f"/api/assignments/{quote(self.key, safe='')}/content")
        self.assertEqual(status, 200)
        self.assertTrue(data["ready"])
        self.assertEqual(data["source_version"], "v1")
        self.assertEqual(data["blocks"][0]["text"], "正文")

    def test_version_conflict_is_409_and_bad_geometry_is_400(self):
        status, data = self.view({"expected_version": "old", "regions": []})
        self.assertEqual(status, 409)
        self.assertEqual(data["code"], "version_conflict")
        status, _ = self.view({"expected_version": "v1", "regions": [
            {"asset_id": self.asset_id, "box": [0.9, 0.1, 0.2, 0.8]}]})
        self.assertEqual(status, 400)

    def test_asset_from_another_version_is_400(self):
        status, _ = self.view({"expected_version": "v1", "regions": [
            {"asset_id": "d" * 64, "box": [0.1, 0.1, 0.8, 0.8]}]})
        self.assertEqual(status, 400)

    def test_valid_multiregion_crop_persists(self):
        regions = [
            {"asset_id": self.asset_id, "box": [0.1, 0.1, 0.5, 0.5]},
            {"asset_id": self.asset_id, "box": [0.2, 0.6, 0.9, 0.95]},
        ]
        status, data = self.view({"expected_version": "v1", "regions": regions})
        self.assertEqual(status, 200, data)
        status, data = self.get(f"/api/assignments/{quote(self.key, safe='')}/content")
        self.assertEqual(len(data["view"]["regions"]), 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
