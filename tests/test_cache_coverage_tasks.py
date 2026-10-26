# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from helpers import IsolatedCase

import cache_store


def hw_row(n: int) -> dict:
    deadline = (datetime.now() + timedelta(days=1 + n % 10)).strftime("%Y-%m-%d %H:%M")
    return {"hwtid": str(1000 + n), "title": f"作业 {n}", "deadline": deadline,
            "submit_url": None, "result_url": None}


class AccountScopeTests(IsolatedCase):
    def test_default_scope_is_unverified_and_blocks_publish(self):
        scope = cache_store.account_scope()
        self.assertTrue(scope.startswith("local-unverified-"))
        ident = cache_store.identity_status(scope)
        self.assertFalse(ident["verified"])
        cov = cache_store.coverage(expected_lids=["1"], tests_requested=False)
        self.assertFalse(cov["publishable"])
        self.assertTrue(any("身份" in x or "账号" in x for x in cov["publish_block_reasons"]))

    def test_identity_marker_binds_and_rejects_silent_account_switch(self):
        scope = cache_store.account_scope()
        cache_store.bind_identity("学号 20260001", scope=scope)
        self.assertTrue(cache_store.identity_status(scope)["verified"])
        with self.assertRaises(cache_store.AccountIdentityConflict):
            cache_store.bind_identity("学号 20260002", scope=scope)
        cache_store.bind_identity("学号 20260002", scope=scope, allow_change=True)
        self.assertTrue(cache_store.identity_status(scope)["verified"])

    def test_explicit_account_scope_is_verified(self):
        scope = self.use_explicit_account("student-a")
        self.assertEqual(cache_store.account_scope(), scope)
        self.assertTrue(cache_store.identity_status(scope)["verified"])


class CoverageBlacklistTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()

    def begin(self, sid, lids, tests=True, version="idx"):
        cache_store.begin_scan(sid, mode="test", expected_lids=lids, requested_lids=lids,
                               tests_requested=tests, course_index_version=version,
                               scope=self.scope)

    def commit(self, sid, lid, hw_status="ok", tests_status="ok", rows=None,
               tests=None, checked_at=None):
        cache_store.commit_course_scan(
            sid, lid=str(lid), course=f"课程{lid}", homeworks=rows or [hw_row(int(lid))],
            tests=tests or [], homework_status=hw_status, tests_status=tests_status,
            index_version="idx", scope=self.scope, checked_at=checked_at)

    def coverage(self, lids=("1", "2"), tests=True, blocked=()):
        return cache_store.coverage(
            self.scope, expected_lids=lids, tests_requested=tests,
            tests_blocked_lids=blocked, current_index_version="idx")

    def test_oldest_required_column_controls_freshness(self):
        now = time.time()
        self.begin("s-oldest", ["1", "2"])
        self.commit("s-oldest", 1, checked_at=now - 100)
        self.commit("s-oldest", 2, checked_at=now)
        cache_store.update_scan("s-oldest", status="complete", finished_at=now)
        cov = self.coverage()
        self.assertTrue(cov["publishable"], cov["publish_block_reasons"])
        self.assertGreaterEqual(cov["age_sec"], 90)

    def test_failed_new_scan_cannot_reuse_previous_success(self):
        now = time.time()
        self.begin("s-good", ["1", "2"])
        self.commit("s-good", 1, checked_at=now)
        self.commit("s-good", 2, checked_at=now)
        cache_store.update_scan("s-good", status="complete", finished_at=now)
        self.assertTrue(self.coverage()["publishable"])
        time.sleep(0.01)
        self.begin("s-failed", ["1", "2"])
        cache_store.update_scan("s-failed", status="error", finished_at=time.time(),
                                error="authentication failed")
        cov = self.coverage()
        self.assertFalse(cov["publishable"])
        self.assertTrue(any("失败" in x for x in cov["publish_block_reasons"]))

    def test_failed_course_is_not_complete_even_if_previous_row_existed(self):
        now = time.time()
        self.begin("s-good2", ["1", "2"])
        self.commit("s-good2", 1, checked_at=now)
        self.commit("s-good2", 2, checked_at=now)
        cache_store.update_scan("s-good2", status="complete", finished_at=now)
        self.begin("s-bad2", ["1", "2"])
        self.commit("s-bad2", 1, checked_at=now)
        self.commit("s-bad2", 2, hw_status="error", checked_at=now)
        cache_store.update_scan("s-bad2", status="partial", finished_at=now)
        cov = self.coverage()
        self.assertFalse(cov["publishable"])
        self.assertIn("2", cov["failed_courses"])

    def test_single_course_refresh_can_restore_completeness(self):
        now = time.time()
        self.begin("s-sub", ["1", "2"])
        self.commit("s-sub", 1, checked_at=now)
        self.commit("s-sub", 2, hw_status="error", checked_at=now)
        cache_store.update_scan("s-sub", status="partial", finished_at=now)
        self.assertFalse(self.coverage()["publishable"])
        self.begin("s-sub2", ["2"])
        self.commit("s-sub2", 2, checked_at=time.time())
        cache_store.update_scan("s-sub2", status="partial", finished_at=time.time())
        cov = self.coverage()
        self.assertTrue(cov["publishable"], cov["publish_block_reasons"])

    def test_removing_test_blacklist_invalidates_old_blocked_state(self):
        now = time.time()
        self.begin("s-blocked", ["1"])
        self.commit("s-blocked", 1, tests_status="blocked", tests=[], checked_at=now)
        cache_store.update_scan("s-blocked", status="complete", finished_at=now)
        self.assertTrue(self.coverage(("1",), blocked={"1"})["publishable"])
        cov = self.coverage(("1",), blocked=set())
        self.assertFalse(cov["publishable"])
        self.assertIn("1", cov["tests_missing_courses"])

    def test_explicit_no_tests_scan_does_not_require_test_column(self):
        now = time.time()
        self.begin("s-notests", ["1"], tests=False)
        self.commit("s-notests", 1, tests_status="disabled", checked_at=now)
        cache_store.update_scan("s-notests", status="complete", finished_at=now)
        cov = self.coverage(("1",), tests=False)
        self.assertTrue(cov["publishable"], cov["publish_block_reasons"])

    def test_current_blacklists_filter_list_and_content_selection(self):
        now = time.time()
        self.begin("s-filter", ["1", "2"])
        self.commit("s-filter", 1, checked_at=now)
        self.commit("s-filter", 2, checked_at=now,
                    tests=[{"test_id": "t2", "title": "测试", "deadline": "2026-12-01 10:00"}])
        cache_store.update_scan("s-filter", status="complete", finished_at=now)
        items = cache_store.load_items(self.scope, exclude_lids={"1"},
                                       exclude_test_lids={"2"})
        self.assertTrue(items)
        self.assertFalse(any(x["lid"] == "1" for x in items))
        self.assertFalse(any(x["kind"] == "test" and x["lid"] == "2" for x in items))
        self.assertTrue(any(x["kind"] == "homework" and x["lid"] == "2" for x in items))


class AccountIsolationRegressionTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()

    def begin(self, sid, lids):
        cache_store.begin_scan(
            sid, mode="test", expected_lids=lids, requested_lids=lids,
            tests_requested=False, course_index_version="idx", scope=self.scope)

    def commit(self, sid, lid, checked_at=None):
        return cache_store.commit_course_scan(
            sid, lid=str(lid), course=f"课程{lid}", homeworks=[hw_row(int(lid))],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version="idx", scope=self.scope, checked_at=checked_at)

    def coverage(self):
        return cache_store.coverage(
            self.scope, expected_lids=["1", "2"], tests_requested=False,
            current_index_version="idx")

    def seed_two_courses(self, sid="s-account-a"):
        now = time.time()
        self.begin(sid, ["1", "2"])
        self.commit(sid, 1, checked_at=now)
        self.commit(sid, 2, checked_at=now)
        cache_store.update_scan(sid, status="complete", finished_at=now)
        return {x["lid"]: x for x in cache_store.load_items(self.scope)}

    def save_body(self, key, *, version="v-a", text="账号 A 正文"):
        return cache_store.save_content(key, {
            "status": "ready", "source_url": "https://course.buct.edu.cn/x",
            "source_hash": version, "source_version": version,
            "blocks": [{"id": "p1", "type": "paragraph", "text": text}],
            "asset_ids": [], "text": text}, scope=self.scope)

    def save_manual_view(self, key, version="v-a"):
        return cache_store.save_view(
            key, expected_version=version, regions=[],
            folded_block_ids=["p1"], summary="人工保留的裁剪摘要")

    def assert_old_body_hidden(self, key):
        item = cache_store.get_item(key, self.scope)
        self.assertTrue(item["content_identity_stale"])
        self.assertEqual(item["content_status"], "identity_changed")
        self.assertFalse(item["content_ready"])
        self.assertFalse(item["content_has_blocks"])
        self.assertEqual(item["content_blocks"], [])
        self.assertEqual(item["content_text"], "")
        self.assertIsNone(item["source_version"])
        self.assertEqual(item["stale_view"]["source_version"], "v-a")
        self.assertEqual(item["stale_view"]["review_status"], "needs_review")
        due_keys = {x["item_key"] for x in cache_store.content_due(
            self.scope, days=30)}
        self.assertIn(key, due_keys)
        exported = cache_store.export_views(self.scope)
        view = next(x for x in exported["views"] if x["item_key"] == key)
        self.assertEqual(view["review_status"], "needs_review")
        self.assertEqual(view["folded_block_ids"], ["p1"])
        self.assertEqual(view["summary"], "人工保留的裁剪摘要")
        with self.assertRaises(cache_store.VersionConflict):
            self.save_manual_view(key)

    def test_account_switch_requires_full_refresh_and_hides_old_body(self):
        items = self.seed_two_courses()
        key = items["1"]["item_key"]
        cache_store.bind_identity("学号 A", scope=self.scope)
        self.save_body(key)
        self.save_manual_view(key)

        # An account-A scan that started before the switch must not restore
        # coverage or resurrect the old body after account B binds.
        self.begin("s-account-a-inflight", ["1", "2"])
        cache_store.bind_identity("学号 B", scope=self.scope, allow_change=True)
        ignored = self.commit("s-account-a-inflight", 2)
        self.assertTrue(ignored.get("ignored"), ignored)
        cache_store.update_scan("s-account-a-inflight", status="complete",
                                finished_at=time.time())
        cov = self.coverage()
        self.assertFalse(cov["publishable"], cov["publish_block_reasons"])
        self.assertIn("2", cov["failed_courses"])
        self.assert_old_body_hidden(key)
        with self.assertRaises(cache_store.VersionConflict):
            cache_store.save_content(key, {"source_version": "late"}, scope=self.scope,
                                     expected_identity_generation=0)
        self.assertFalse(cache_store.record_content_error(
            key, "late failure", scope=self.scope, expected_identity_generation=0))
        self.assertFalse(cache_store.record_content_error(key, "new fetch failed", scope=self.scope))
        self.assert_old_body_hidden(key)

        # Refreshing only one course is insufficient: course 2's old coverage
        # was invalidated, not merely the old scan_runs row.
        self.begin("s-account-b-one", ["1"])
        self.commit("s-account-b-one", 1)
        cache_store.update_scan("s-account-b-one", status="complete",
                                finished_at=time.time())
        cov = self.coverage()
        self.assertFalse(cov["publishable"], cov["publish_block_reasons"])
        self.assertIn("2", cov["failed_courses"])

        # A full two-course refresh restores publication coverage.
        self.begin("s-account-b-full", ["1", "2"])
        self.commit("s-account-b-full", 1)
        self.commit("s-account-b-full", 2)
        cache_store.update_scan("s-account-b-full", status="complete",
                                finished_at=time.time())
        cov = self.coverage()
        self.assertTrue(cov["publishable"], cov["publish_block_reasons"])

        # Even an identical source_version belongs to a new identity and must
        # not carry the old body forward; the manual view remains reviewable.
        self.save_body(key, version="v-a", text="账号 B 正文")
        item = cache_store.get_item(key, self.scope)
        self.assertTrue(item["content_ready"])
        self.assertEqual(item["content_text"], "账号 B 正文")
        self.assertNotEqual(item["source_version"], "v-a")
        with self.assertRaises(cache_store.VersionConflict):
            self.save_manual_view(key, version="v-a")
        self.assertNotIn("content_identity_stale", item)
        self.assertNotIn("view", item)
        self.assertEqual(item["stale_view"]["review_status"], "needs_review")

    def test_scan_cli_auth_failure_invalidates_other_course_coverage(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        import scrape
        self.seed_two_courses("cli-before")
        ctx = MagicMock()
        ctx.pages = [MagicMock()]
        args = SimpleNamespace(scan_id="cli-failure", debug=False,
                               deadline_only=True, with_tests=False)
        with patch.object(scrape, "sync_playwright", return_value=MagicMock()), \
                patch.object(scrape, "make_context", return_value=ctx), \
                patch.object(scrape, "wait_authenticated", return_value=False), \
                patch.object(scrape, "dump"), \
                patch.object(scrape, "load_course_index", return_value={"1": "A", "2": "B"}), \
                patch.object(scrape, "blacklisted_lids", return_value=set()):
            self.assertEqual(scrape._run_scan(args), 1)
        self.begin("cli-after-one", ["1"])
        self.commit("cli-after-one", 1)
        cache_store.update_scan("cli-after-one", status="complete", finished_at=time.time())
        self.assertFalse(self.coverage()["publishable"])
        self.assertIn("2", self.coverage()["failed_courses"])

    def test_content_entry_auth_failure_invalidates_scope(self):
        from unittest.mock import MagicMock, patch
        import scrape
        items = self.seed_two_courses("content-before")
        key = items["1"]["item_key"]
        self.save_body(key)
        self.save_manual_view(key)
        with patch.object(scrape, "page_is_login", return_value=True):
            result = scrape.get_hw_content(MagicMock(), "7001", False, lid="1")
        self.assertFalse(result["ok"])
        self.assert_old_body_hidden(key)
        self.begin("content-after-one", ["1"])
        self.commit("content-after-one", 1)
        cache_store.update_scan("content-after-one", status="complete", finished_at=time.time())
        self.assertFalse(self.coverage()["publishable"])

    def test_auth_invalidation_requires_full_refresh_and_hides_old_body(self):
        items = self.seed_two_courses("s-auth-a")
        key = items["1"]["item_key"]
        self.save_body(key)
        self.save_manual_view(key)

        self.begin("s-auth-inflight", ["1", "2"])
        cache_store.invalidate_scope_scans("认证上下文失效", scope=self.scope)
        ignored = self.commit("s-auth-inflight", 2)
        self.assertTrue(ignored.get("ignored"), ignored)
        cov = self.coverage()
        self.assertFalse(cov["publishable"], cov["publish_block_reasons"])
        self.assert_old_body_hidden(key)

        self.begin("s-auth-one", ["1"])
        self.commit("s-auth-one", 1)
        cache_store.update_scan("s-auth-one", status="complete",
                                finished_at=time.time())
        cov = self.coverage()
        self.assertFalse(cov["publishable"], cov["publish_block_reasons"])
        self.assertIn("2", cov["failed_courses"])

        self.begin("s-auth-full", ["1", "2"])
        self.commit("s-auth-full", 1)
        self.commit("s-auth-full", 2)
        cache_store.update_scan("s-auth-full", status="complete",
                                finished_at=time.time())
        cov = self.coverage()
        self.assertTrue(cov["publishable"], cov["publish_block_reasons"])


class ContentSelectionAndViewTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()

    def seed_many(self, count=15):
        lids = [str(i) for i in range(1, count + 1)]
        cache_store.begin_scan("many", mode="test", expected_lids=lids,
                               requested_lids=lids, tests_requested=False,
                               course_index_version="idx", scope=self.scope)
        for i in range(1, count + 1):
            cache_store.commit_course_scan(
                "many", lid=str(i), course=f"课程{i}", homeworks=[hw_row(i)], tests=[],
                homework_status="ok", tests_status="disabled", index_version="idx",
                scope=self.scope)
        cache_store.update_scan("many", status="complete", finished_at=time.time())
        return cache_store.load_items(self.scope)

    def test_default_selection_is_not_silently_capped_at_twelve(self):
        items = self.seed_many(15)
        due = cache_store.content_due(self.scope, days=30, limit=None)
        self.assertEqual(len(due), 15)
        capped = cache_store.content_due(self.scope, days=30, limit=12)
        self.assertEqual(len(capped), 12)
        one = cache_store.content_due(self.scope, days=30, limit=None,
                                      item_keys=[items[0]["item_key"]])
        self.assertEqual([x["item_key"] for x in one], [items[0]["item_key"]])
        excluded = cache_store.content_due(self.scope, days=30, limit=None,
                                           exclude_lids={items[0]["lid"]})
        self.assertNotIn(items[0]["lid"], {x["lid"] for x in excluded})

    def test_crop_save_validates_version_asset_and_geometry(self):
        item = self.seed_many(1)[0]
        key = item["item_key"]
        asset_id = "a" * 64
        saved = cache_store.save_content(key, {
            "status": "ready", "source_url": "https://course.buct.edu.cn/x",
            "source_hash": "h1", "source_version": "v1",
            "blocks": [{"id": "p1", "type": "paragraph", "text": "x"}],
            "asset_ids": [asset_id], "assets": [], "text": "x"}, scope=self.scope)
        self.assertEqual(saved["source_version"], "v1")
        with self.assertRaises(cache_store.VersionConflict):
            cache_store.save_view(key, expected_version="old", regions=[])
        with self.assertRaises(ValueError):
            cache_store.save_view(key, expected_version="v1", regions=[
                {"asset_id": asset_id, "box": [0.8, 0.1, 0.2, 0.9]}])
        with self.assertRaises(ValueError):
            cache_store.save_view(key, expected_version="v1", regions=[
                {"asset_id": "b" * 64, "box": [0.1, 0.1, 0.9, 0.9]}])
        result = cache_store.save_view(key, expected_version="v1", regions=[
            {"asset_id": asset_id, "box": [0.1, 0.2, 0.8, 0.9], "origin": "manual"}])
        self.assertEqual(result["regions"][0]["box"], [0.1, 0.2, 0.8, 0.9])
        cache_store.save_content(key, {
            "status": "ready", "source_hash": "h2", "source_version": "v2",
            "blocks": [{"id": "p1", "type": "paragraph", "text": "new"}],
            "asset_ids": [asset_id], "assets": [], "text": "new"}, scope=self.scope)
        with self.assertRaises(cache_store.VersionConflict):
            cache_store.save_view(key, expected_version="v1", regions=[])
        item2 = cache_store.get_item(key, self.scope)
        self.assertEqual(item2["stale_view"]["source_version"], "v1")


class TaskRegistryTests(IsolatedCase):
    def test_dedupe_scan_id_and_orphan_recovery(self):
        tid, deduped = cache_store.register_task("task-a", "sig", "fetch", {})
        self.assertFalse(deduped)
        same, deduped = cache_store.register_task("task-b", "sig", "fetch", {})
        self.assertTrue(deduped)
        self.assertEqual(same, tid)
        cache_store.update_task(tid, "running", scan_id="scan-a")
        rec = cache_store.task_record(tid)
        self.assertEqual(rec["scan_id"], "scan-a")
        cache_store.update_task(tid, "done", scan_id="scan-a")
        with cache_store.connection(write=True) as con:
            con.execute(
                """INSERT INTO task_registry(task_id,signature,op,params_json,status,
                   created_at,owner_pid,owner_started) VALUES(?,?,?,?,?,?,?,?)""",
                ("orphan", "sig-orphan", "fetch", "{}", "running",
                 time.time() - 60, 99999999, 1))
        self.assertGreaterEqual(cache_store.recover_orphan_tasks(), 1)
        self.assertEqual(cache_store.task_record("orphan")["status"], "error")

    def test_task_progress_never_falls_back_to_another_scan(self):
        import server
        cache_store.begin_scan("other-scan", mode="test", expected_lids=["1"],
                               requested_lids=["1"], tests_requested=False,
                               course_index_version="idx", scope="test-account")
        self.assertIsNone(server.task_progress({"status": "running"}))
        progress = server.task_progress({"status": "running", "scan_id": "other-scan"})
        self.assertEqual(progress["scan_id"], "other-scan")


if __name__ == "__main__":
    import unittest
    unittest.main()
