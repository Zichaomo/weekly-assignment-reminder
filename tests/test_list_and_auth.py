# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from helpers import FakeContext, FakePage, IsolatedCase

import scrape


class ListClassificationTests(IsolatedCase):
    def test_header_only_is_not_a_legitimate_empty(self):
        page = FakePage(tables=[[["标题", "截止时间", "发布人"]]], text="")
        status, why = scrape.classify_list_page(page, "homework", [])
        self.assertEqual(status, "invalid")
        self.assertIn("只有目标表头", why)

    def test_header_plus_unparsed_data_rows_is_incomplete(self):
        page = FakePage(tables=[[["标题", "截止时间"], ["坏行", "无法解析"]]], text="")
        status, why = scrape.classify_list_page(page, "homework", [])
        self.assertEqual(status, "incomplete")
        self.assertIn("一条也未解析", why)

    def test_partial_parse_blocks_cache_replacement(self):
        page = FakePage(tables=[[["标题", "截止时间"], ["A", "2026-11-01"],
                                 ["B", "坏日期"], ["C", "坏日期"]]], text="")
        status, why = scrape.classify_list_page(page, "homework", [{"hwtid": "1"}])
        self.assertEqual(status, "incomplete")
        self.assertIn("1/3", why)

    def test_javascript_next_control_is_incomplete(self):
        page = FakePage(
            tables=[[["标题", "截止时间"], ["A", "2026-11-01"]]], text="",
            anchors=[{"text": "下一页", "href": "javascript:void(0)",
                      "onclick": "nextPage()", "disabled": False}])
        status, why = scrape.classify_list_page(page, "homework", [{"hwtid": "1"}])
        self.assertEqual(status, "incomplete")
        self.assertIn("下一页", why)

    def test_target_table_is_not_assumed_to_be_the_first_table(self):
        page = FakePage(tables=[[["导航", "说明"]],
                                [["标题", "截止时间", "发布人"],
                                 ["作业 A", "2026-11-01", "老师"]]], text="")
        status, why = scrape.classify_list_page(
            page, "homework", [{"hwtid": "77", "title": "作业 A"}])
        self.assertEqual(status, "ok", why)

    def test_explicit_empty_with_target_header_is_valid(self):
        page = FakePage(tables=[[["标题", "截止时间"]]], text="暂无作业")
        status, why = scrape.classify_list_page(page, "homework", [])
        self.assertEqual(status, "empty", why)

    def test_explicit_empty_without_target_table_is_not_valid(self):
        page = FakePage(tables=[], text="暂无作业")
        status, _ = scrape.classify_list_page(page, "homework", [])
        self.assertEqual(status, "invalid")


class CourseContextTests(IsolatedCase):
    def test_lid_in_course_entry_url_is_required_and_used(self):
        good = FakePage(url="https://course.buct.edu.cn/meol/lesson/enter_course.jsp?lid=123&t=hw")
        ok, why = scrape.validate_course_context(good, "123", "高数")
        self.assertTrue(ok, why)
        wrong = FakePage(url="https://course.buct.edu.cn/meol/lesson/enter_course.jsp?lid=999&t=hw",
                         text="高数")
        ok, _ = scrape.validate_course_context(wrong, "123", "高数")
        self.assertFalse(ok)

    def test_theol_newpage_course_redirect_requires_matching_course_id(self):
        for course_id, expected in (("123", True), ("999", False)):
            page = FakePage(text="高数 课程作业",
                            url="https://course.buct.edu.cn/meol/jpk/course/layout/newpage/index.jsp"
                                + "?courseId=" + course_id)
            self.assertEqual(scrape.validate_course_context(page, "123", "高数")[0], expected)

    def test_suffix_lookalike_domain_is_rejected(self):
        page = FakePage(url="https://evilbuct.edu.cn/meol/lesson/enter_course.jsp?lid=123")
        ok, why = scrape.validate_course_context(page, "123", "")
        self.assertFalse(ok)
        self.assertIn("域名异常", why)

    def test_foreign_page_cannot_pass_by_name_alone(self):
        page = FakePage(url="https://course.buct.edu.cn/meol/homepage/common/sso_login.jsp",
                        text="高数", selectors={})
        ok, _ = scrape.validate_course_context(page, "123", "高数")
        self.assertFalse(ok)

    def test_authenticated_requires_page_evidence_not_cookie(self):
        page = FakePage(text="", url="https://course.buct.edu.cn/meol/homepage/common/sso_login.jsp")
        self.assertFalse(scrape.wait_authenticated(page, FakeContext(page), timeout=0.01))
        page2 = FakePage(text="我的课程", url="https://course.buct.edu.cn/meol/index.jsp")
        self.assertTrue(scrape.wait_authenticated(page2, FakeContext(page2), timeout=0.01))

    def test_personal_statistics_override_incidental_login_link(self):
        page = FakePage(text="在线总时长 12小时 登录时间 今天 统一身份认证登录",
                        url="https://course.buct.edu.cn/meol/personal.do")
        self.assertFalse(scrape.page_is_login(page))
        self.assertTrue(scrape.page_authenticated(page))

    def test_login_return_query_does_not_make_personal_page_a_login_page(self):
        page = FakePage(text="在线总时长 登录时间 统一身份认证登录",
                        url="https://course.buct.edu.cn/meol/personal.do?back=sso_login.jsp")
        self.assertTrue(scrape.page_authenticated(page))

    def test_visible_password_form_overrides_personal_statistics(self):
        page = FakePage(text="在线总时长 登录时间 统一身份认证登录",
                        url="https://course.buct.edu.cn/meol/personal.do",
                        selector_values={"input[type='password']": True})
        self.assertTrue(scrape.page_is_login(page))
        self.assertFalse(scrape.page_authenticated(page))

    def test_sidportal_login_path_overrides_weak_positive_text(self):
        page = FakePage(text="我的课程 退出",
                        url="https://sidportal.buct.edu.cn/login?return=personal.do")
        self.assertFalse(scrape.page_authenticated(page))

    def test_login_landing_text_not_overridden_by_weak_positive_words(self):
        page = FakePage(text="统一身份认证登录 我的课程 退出",
                        url="https://course.buct.edu.cn/meol/index.jsp")
        self.assertFalse(scrape.page_authenticated(page))

    def test_foreign_personal_page_text_cannot_authenticate(self):
        page = FakePage(text="在线总时长 登录时间",
                        url="https://evilbuct.edu.cn/meol/personal.do")
        self.assertFalse(scrape.page_authenticated(page))

    def test_login_page_marker_overrides_authenticated_words(self):
        page = FakePage(text="统一身份认证登录 我的课程",
                        url="https://course.buct.edu.cn/meol/homepage/common/sso_login.jsp")
        self.assertFalse(scrape.page_authenticated(page))


if __name__ == "__main__":
    unittest.main()
