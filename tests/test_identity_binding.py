# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import time
from unittest.mock import Mock

from helpers import FakePage, IsolatedCase

import cache_store
import scrape


BASE = "https://course.buct.edu.cn"
COURSE = BASE + "/meol/lesson/enter_course.jsp?lid=123&t=hw"
PERSONAL = BASE + "/meol/personal.do"


class PlatformIdentityTests(IsolatedCase):
    def test_teacher_names_on_course_pages_never_become_identity(self):
        for teacher in ("脱敏老师甲", "脱敏老师乙"):
            with self.subTest(teacher=teacher):
                page = FakePage(
                    text=("在线总时长 登录时间 课程作业\n教师信息\n"
                          f"教师姓名：{teacher}"),
                    url=COURSE)
                self.assertIsNone(scrape.platform_identity(page))

    def test_ordinary_name_text_on_course_page_is_not_identity(self):
        page = FakePage(text="课程作业\n姓名：脱敏同学", url=COURSE)
        self.assertIsNone(scrape.platform_identity(page))

    def test_authenticated_personal_page_reads_independent_label(self):
        page = FakePage(
            text="在线总时长 12小时 登录时间 今天\n学号：20260001\n姓名：脱敏同学",
            url=PERSONAL)
        self.assertEqual(scrape.platform_identity(page), "20260001")

    def test_teacher_label_on_personal_page_is_not_an_account_label(self):
        page = FakePage(
            text="在线总时长 12小时 登录时间 今天\n教师信息\n教师姓名：脱敏老师",
            url=PERSONAL)
        self.assertIsNone(scrape.platform_identity(page))

    def test_body_fallback_requires_authenticated_native_personal_page(self):
        unauthenticated = FakePage(text="学号：20260001", url=PERSONAL)
        login_form = FakePage(
            text="在线总时长 登录时间\n学号：20260001", url=PERSONAL,
            selector_values={"input[type='password']": True})
        foreign = FakePage(
            text="在线总时长 登录时间\n学号：20260001",
            url="https://evilbuct.edu.cn/meol/personal.do")
        for page in (unauthenticated, login_form, foreign):
            with self.subTest(url=page.url):
                self.assertIsNone(scrape.platform_identity(page))

    def test_trusted_account_element_is_preferred_and_changes_still_conflict(self):
        scope = cache_store.account_scope()
        first = FakePage(text="课程作业", url=COURSE,
                         selector_values={"#userName": "学号 甲"})
        second = FakePage(text="课程作业", url=COURSE,
                          selector_values={"#userName": "学号 乙"})
        self.assertEqual(scrape.platform_identity(first), "学号 甲")
        scrape.bind_page_identity(first, scope=scope)
        self.assertTrue(cache_store.identity_status(scope)["verified"])
        with self.assertRaises(cache_store.AccountIdentityConflict):
            scrape.bind_page_identity(second, scope=scope)
        self.assertEqual(cache_store.identity_status(scope)["marker_hash"],
                         hashlib.sha256("学号 甲".encode("utf-8")).hexdigest()[:24])


class OwnProfileIdentityTests(IsolatedCase):
    def profile_case(self, pairs=None, href='/meol/popups/viewstudent_info.jsp', final=None):
        page = FakePage(text='在线总时长 登录时间', url=PERSONAL,
                        selector_values={'.name a.info.chgright[href]': href})
        profile = FakePage(url=BASE + href)
        profile.eval_on_selector_all = Mock(return_value=pairs or [
            ['用户名：', '20260001'], ['学号：', '20260001'], ['真实姓名：', '脱敏同学']])
        profile.close = Mock()
        if final:
            profile.goto = lambda *a, **kw: setattr(profile, 'url', final)
        page.context = Mock()
        page.context.new_page.return_value = profile
        return page, profile

    def test_own_profile_reads_stable_id_and_preserves_personal_page(self):
        page, profile = self.profile_case()
        self.assertEqual(scrape.resolve_platform_identity(page), '20260001')
        self.assertEqual(page.url, PERSONAL)
        profile.close.assert_called_once()
        scrape.bind_page_identity(page)
        self.assertTrue(cache_store.identity_status()['verified'])

    def test_teacher_or_other_origin_links_are_never_opened(self):
        for href in ('https://evil.example/meol/popups/viewstudent_info.jsp',
                     'http://course.buct.edu.cn/meol/popups/viewstudent_info.jsp',
                     'https://course.buct.edu.cn:444/meol/popups/viewstudent_info.jsp',
                     '/meol/popups/viewteacher_info.jsp'):
            page, _ = self.profile_case(href=href)
            self.assertIsNone(scrape.resolve_platform_identity(page))
            page.context.new_page.assert_not_called()

    def test_course_and_login_pages_cannot_follow_own_profile_link(self):
        page, _ = self.profile_case()
        for url in (COURSE, BASE + '/meol/login.jsp'):
            page.url = url
            self.assertIsNone(scrape.resolve_platform_identity(page))
        page.context.new_page.assert_not_called()

    def test_redirect_login_or_foreign_page_is_rejected_and_closed(self):
        for final in ('https://sidportal.buct.edu.cn/login',
                      'https://evil.example/meol/popups/viewstudent_info.jsp'):
            page, profile = self.profile_case(final=final)
            self.assertIsNone(scrape.resolve_platform_identity(page))
            profile.close.assert_called_once()

    def test_missing_ambiguous_or_invalid_account_does_not_fall_back_to_name(self):
        for pairs in ([['姓名：', '脱敏同学']],
                      [['用户名', 'account-a'], ['用户名', 'account-b']],
                      [['用户名', 'invalid account']]):
            page, _ = self.profile_case(pairs=pairs)
            self.assertIsNone(scrape.resolve_platform_identity(page))

    def test_profile_timeout_closes_page_and_leaves_identity_unverified(self):
        page, profile = self.profile_case()
        profile.goto = Mock(side_effect=TimeoutError())
        self.assertIsNone(scrape.resolve_platform_identity(page))
        profile.close.assert_called_once()
        self.assertFalse(cache_store.identity_status()['verified'])

    def test_real_account_switch_still_conflicts(self):
        page, _ = self.profile_case()
        scrape.bind_page_identity(page)
        other, _ = self.profile_case(pairs=[['用户名', '20260002']])
        with self.assertRaises(cache_store.AccountIdentityConflict):
            scrape.bind_page_identity(other)


class LegacyAutoIdentityMigrationTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = cache_store.account_scope()
        self.item_key = self._seed_content_and_old_scan()
        legacy = hashlib.sha256("教师姓名：脱敏老师".encode("utf-8")).hexdigest()[:24]
        with cache_store.connection(write=True) as con:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        (f"identity:{self.scope}", legacy))
            con.execute("DELETE FROM meta WHERE key=?",
                        (f"identity_source:{self.scope}",))

    def _meta(self, key):
        with cache_store.connection() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _seed_content_and_old_scan(self):
        cache_store.begin_scan(
            "legacy-before", mode="test", expected_lids=["123"],
            requested_lids=["123"], tests_requested=False,
            course_index_version="idx", scope=self.scope)
        cache_store.commit_course_scan(
            "legacy-before", lid="123", course="脱敏课程",
            homeworks=[{"hwtid": "9001", "title": "脱敏作业",
                        "deadline": "2099-01-02 10:00"}],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version="idx", scope=self.scope)
        cache_store.update_scan("legacy-before", status="complete",
                                finished_at=time.time())
        item = cache_store.load_items(self.scope)[0]
        cache_store.save_content(item["item_key"], {
            "status": "ready", "source_hash": "legacy", "source_version": "v-legacy",
            "blocks": [{"id": "p1", "type": "paragraph", "text": "旧账号正文"}],
            "asset_ids": [], "text": "旧账号正文"}, scope=self.scope)
        cache_store.save_view(
            item["item_key"], expected_version="v-legacy", regions=[],
            folded_block_ids=["p1"], summary="人工保留摘要")
        cache_store.begin_scan(
            "legacy-inflight", mode="test", expected_lids=["123"],
            requested_lids=["123"], tests_requested=False,
            course_index_version="idx", scope=self.scope)
        return item["item_key"]

    def test_legacy_auto_identity_migrates_once_and_requires_rescan(self):
        ident = cache_store.identity_status(self.scope)
        self.assertFalse(ident["verified"])
        self.assertIsNone(ident["marker_hash"])
        generation = self._meta(f"identity_generation:{self.scope}")
        self.assertEqual(generation, "1")
        self.assertIsNone(self._meta(f"identity:{self.scope}"))

        self.assertFalse(cache_store.migrate_identity_state(self.scope))
        cache_store.identity_status(self.scope)
        cache_store.load_items(self.scope)
        self.assertEqual(self._meta(f"identity_generation:{self.scope}"), "1")

        item = cache_store.get_item(self.item_key, self.scope)
        self.assertTrue(item["content_identity_stale"])
        self.assertFalse(item["content_ready"])
        self.assertEqual(item["content_text"], "")
        self.assertEqual(item["stale_view"]["review_status"], "needs_review")
        exported = cache_store.export_views(self.scope)
        view = next(v for v in exported["views"] if v["item_key"] == self.item_key)
        self.assertEqual(view["summary"], "人工保留摘要")
        self.assertEqual(view["folded_block_ids"], ["p1"])

        cov = cache_store.coverage(
            self.scope, expected_lids=["123"], tests_requested=False,
            current_index_version="idx")
        self.assertFalse(cov["publishable"])

        bound = cache_store.bind_identity("学号 新账号", scope=self.scope)
        self.assertFalse(bound["changed"])
        self.assertTrue(cache_store.identity_status(self.scope)["verified"])
        ignored = cache_store.commit_course_scan(
            "legacy-inflight", lid="123", course="脱敏课程",
            homeworks=[{"hwtid": "9001", "title": "旧扫描写回",
                        "deadline": "2099-01-02 10:00"}],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version="idx", scope=self.scope)
        self.assertTrue(ignored["ignored"])

        cache_store.begin_scan(
            "fresh-after-migration", mode="test", expected_lids=["123"],
            requested_lids=["123"], tests_requested=False,
            course_index_version="idx", scope=self.scope)
        cache_store.commit_course_scan(
            "fresh-after-migration", lid="123", course="脱敏课程",
            homeworks=[{"hwtid": "9001", "title": "新作业",
                        "deadline": "2099-01-02 10:00"}],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version="idx", scope=self.scope)
        cache_store.update_scan("fresh-after-migration", status="complete",
                                finished_at=time.time())
        cov = cache_store.coverage(
            self.scope, expected_lids=["123"], tests_requested=False,
            current_index_version="idx")
        self.assertTrue(cov["publishable"], cov["publish_block_reasons"])


if __name__ == "__main__":
    import unittest
    unittest.main()
