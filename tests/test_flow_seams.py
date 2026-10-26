# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from helpers import FakePage, IsolatedCase

import cache_store
import hub
import manual
import scrape
import weekly


def hw_row(n=1, submitted=None):
    return {"hwtid": str(7000 + n), "title": f"作业 {n}",
            "deadline": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M"),
            "submit_url": None, "result_url": None, "submitted": submitted}


class ContentFlowSeams(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()
        cache_store.begin_scan("flow-scan", mode="test", expected_lids=["1"],
                               requested_lids=["1"], tests_requested=False,
                               course_index_version="idx", scope=self.scope)
        cache_store.commit_course_scan(
            "flow-scan", lid="1", course="高数", homeworks=[hw_row()], tests=[],
            homework_status="ok", tests_status="disabled", index_version="idx",
            scope=self.scope)
        cache_store.update_scan("flow-scan", status="complete", finished_at=time.time())
        self.item = cache_store.load_items(self.scope)[0]
        cache_store.save_content(self.item["item_key"], {
            "status": "ready", "source_url": "https://course.buct.edu.cn/x",
            "source_hash": "h", "source_version": "v1",
            "blocks": [{"id": "p1", "type": "paragraph", "text": "已有正文"}],
            "asset_ids": [], "text": "已有正文"}, scope=self.scope)

    def test_fresh_cached_content_returns_without_browser(self):
        with patch.object(hub, "FetchLock", side_effect=AssertionError("browser lock used")), \
                patch.object(scrape, "sync_playwright", side_effect=AssertionError("browser used")):
            out = hub.run_content(hwtid=str(self.item["hwtid"]))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["cached"], 1)
        self.assertEqual(out["content_text"], "已有正文")

    def test_unknown_hwtid_is_clear_error_not_empty_success(self):
        out = hub.run_content(hwtid="does-not-exist")
        self.assertFalse(out["ok"])
        self.assertIn("does-not-exist", out["error"])
        self.assertIn("hwtid", out["error"])

    def test_explicit_cached_item_does_not_start_browser(self):
        with patch.object(scrape, "sync_playwright", side_effect=AssertionError("browser used")):
            out = hub.run_content(item_keys=[self.item["item_key"]])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["cached"], 1)


class ListAndAudienceSeams(IsolatedCase):
    def test_no_tests_never_requests_test_column(self):
        page = FakePage(text="课程作业 截止时间",
                        url="https://course.buct.edu.cn/meol/lesson/enter_course.jsp?lid=11&t=hw")
        calls = []

        def column(_page, label, **kwargs):
            calls.append(label)
            return "https://course.buct.edu.cn/hw"

        with patch.object(scrape, "get_column_href", side_effect=column), \
                patch.object(scrape, "parse_hw_table",
                             return_value=[{"hwtid": "1", "title": "A",
                                            "deadline": "2026-12-01 10:00"}]), \
                patch.object(scrape, "classify_list_page",
                             return_value=("ok", "ok")):
            rows, tests, notes, statuses = scrape.fetch_course_both(
                None, page, "11", "高数", want_tests=False, empty_retries=0,
                return_status=True)
        self.assertEqual(calls, ["课程作业"])
        self.assertEqual(statuses["tests"], "disabled")
        self.assertEqual(len(rows), 1)
        self.assertEqual(tests, [])

    def test_submitted_homework_stays_in_list(self):
        rows = hub.build_rows([hw_row(submitted=True)], [], days=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "平台")

    def test_elective_is_excluded_only_from_class_audience(self):
        items = [{"lid": "1", "course": "必修", "title": "A"},
                 {"lid": "2", "course": "选修", "title": "B"},
                 {"course": "群通知", "title": "C", "source": "manual"}]
        class_items, personal = weekly.split_items(items, {"2"})
        self.assertEqual([x["title"] for x in class_items], ["A", "C"])
        self.assertEqual([x["title"] for x in personal], ["A", "B", "C"])


class ManualIndependenceTests(IsolatedCase):
    def test_manual_crud_works_without_platform_cache(self):
        target = self.root / "manual.json"
        with patch.object(manual, "MANUAL_FILE", target), \
                patch.object(hub, "MANUAL_FILE", target):
            code = manual.cmd_add(SimpleNamespace(
                course="高数", title="手动作业", deadline="2026-12-31 23:59", note="群通知"))
            self.assertEqual(code, 0)
            self.assertEqual(len(manual.load()), 1)
            manual.cmd_done(SimpleNamespace(id=1))
            self.assertTrue(manual.load()[0]["done"])
            self.assertEqual(hub.load_manual_raw()[0]["title"], "手动作业")


class IdentityAndExportTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()

    def _good_scan(self):
        cache_store.begin_scan("identity-good", mode="test", expected_lids=["1"],
                               requested_lids=["1"], tests_requested=False,
                               course_index_version="idx", scope=self.scope)
        cache_store.commit_course_scan(
            "identity-good", lid="1", course="高数", homeworks=[hw_row()], tests=[],
            homework_status="ok", tests_status="disabled", index_version="idx",
            scope=self.scope)
        cache_store.update_scan("identity-good", status="complete", finished_at=time.time())

    def test_explicit_empty_expected_never_falls_back_to_old_scope(self):
        self._good_scan()
        cov = cache_store.coverage(self.scope, expected_lids=[], tests_requested=False,
                                   current_index_version="idx")
        self.assertFalse(cov["publishable"])
        self.assertEqual(cov["expected_courses"], 0)
        self.assertTrue(any("课程范围为空" in x for x in cov["publish_block_reasons"]))

    def test_identity_change_invalidates_all_old_success(self):
        self._good_scan()
        cache_store.bind_identity("学号 A", scope=self.scope)
        self.assertTrue(cache_store.coverage(
            self.scope, expected_lids=["1"], tests_requested=False,
            current_index_version="idx")["publishable"])
        cache_store.bind_identity("学号 B", scope=self.scope, allow_change=True)
        cov = cache_store.coverage(self.scope, expected_lids=["1"], tests_requested=False,
                                   current_index_version="idx")
        self.assertFalse(cov["publishable"])
        self.assertTrue(any("身份已变更" in x for x in cov["publish_block_reasons"]))
        self.assertEqual(cache_store.load_items(self.scope)[0]["title"], "作业 1")

    def test_export_includes_needs_review_manual_history(self):
        self._good_scan()
        item = cache_store.load_items(self.scope)[0]
        cache_store.save_content(item["item_key"], {
            "status": "ready", "source_hash": "h", "source_version": "v1",
            "blocks": [{"id": "p1", "type": "paragraph", "text": "x"}],
            "asset_ids": [], "text": "x"}, scope=self.scope)
        cache_store.save_view(item["item_key"], expected_version="v1", regions=[],
                              review_status="needs_review")
        exported = cache_store.export_views(self.scope)
        self.assertEqual(len(exported["views"]), 1)
        self.assertEqual(exported["views"][0]["review_status"], "needs_review")
