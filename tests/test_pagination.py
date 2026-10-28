# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from helpers import FakePage, IsolatedCase

import cache_store
import scrape


BASE = "https://course.buct.edu.cn"
HW_LIST = BASE + "/meol/common/hw/student/hwtask.jsp"
PAGE1 = HW_LIST + "?courseId=123"
PAGE2 = HW_LIST + "?s_gotopage=2"
TRANSFER = (BASE + "/meol/jpk/course/course_column_preview_transfer.jsp"
            + "?columnId=desensitized")
HEADER = ["标题", "截止时间", "发布人", "分数", "统计信息", "提交作业", "查看结果"]


def _cell(text, href=None):
    return {"text": text, "href": href}


def _hw_row(i):
    return [
        _cell(f"作业 {i}", HW_LIST.replace("hwtask.jsp", "hwtask.view.jsp")
              + f"?hwtid={i}"),
        _cell("2026-12-01 10:00"),
        _cell("老师"),
        _cell("100"),
        _cell("已交 1/2"),
        _cell("提交"),
        _cell("查看"),
    ]


def hw_page(ids, total, *, next_href=None, anchors=None, text=None,
            header_only=False):
    row_cells = [] if header_only else [_hw_row(i) for i in ids]
    table = [HEADER] + [[cell["text"] for cell in row] for row in row_cells]
    page_anchors = list(anchors or [])
    if next_href:
        page_anchors.insert(0, {"text": "下一页", "href": next_href,
                                "onclick": "", "disabled": False})
    body = text if text is not None else f"共{total} 条记录 [首页] [下一页] [尾页]"
    return {"tables": [table], "row_cells": row_cells, "text": body,
            "anchors": page_anchors}


class PagedFakePage:
    """A desensitized route-based page: no network, no credentials."""

    def __init__(self, routes, start_url):
        self.routes = {url: dict(spec) for url, spec in routes.items()}
        self.url = start_url
        self.gotos = []

    def _route(self):
        try:
            return self.routes[self.url]
        except KeyError as exc:
            raise RuntimeError(f"offline fixture missing route: {self.url}") from exc

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url
        route = self.routes.get(url) or {}
        if route.get("redirect"):
            self.url = route["redirect"]
        return None

    def inner_text(self, selector, timeout=None):
        return self._route().get("text", "")

    def eval_on_selector_all(self, selector, script):
        route = self._route()
        if selector == "table":
            return route.get("tables", [])
        if selector == "table tr":
            return route.get("row_cells", [])
        if selector in ("a,button,input", "a"):
            return route.get("anchors", [])
        return []

    def eval_on_selector(self, selector, script):
        return False

    def close(self):
        pass


class SlowParsePagedFakePage(PagedFakePage):
    """Advance a fake monotonic clock while the table DOM is parsed."""

    def __init__(self, routes, start_url, clock, advance):
        super().__init__(routes, start_url)
        self.clock = clock
        self.advance = advance

    def eval_on_selector_all(self, selector, script):
        result = super().eval_on_selector_all(selector, script)
        if selector == "table tr":
            self.clock[0] += self.advance
        return result


class PaginationFlowTests(IsolatedCase):
    def collect(self, routes, start=PAGE1, kind="homework"):
        page = PagedFakePage(routes, start)
        rows, status, why, landed = scrape._collect_list_pages(
            page, kind, start, "123", time.monotonic() + 30,
            False, "offline")
        return page, rows, status, why, landed

    def test_34_rows_across_two_pages_is_complete(self):
        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="hwtask.jsp?s_gotopage=2",
                           anchors=[{"text": "尾页", "href": "hwtask.jsp?s_page=last",
                                     "onclick": "", "disabled": False}]),
            PAGE2: hw_page(range(31, 35), 34,
                           anchors=[{"text": "上一页",
                                     "href": "hwtask.jsp?s_gotopage=1",
                                     "onclick": "", "disabled": False},
                                    {"text": "尾页", "href": "hwtask.jsp?s_page=last",
                                     "onclick": "", "disabled": False}]),
        }
        page, rows, status, why, landed = self.collect(routes)
        self.assertEqual(status, "ok", why)
        self.assertEqual(len(rows), 34)
        self.assertEqual(len({r["hwtid"] for r in rows}), 34)
        self.assertEqual(page.gotos, [PAGE1, PAGE2])
        self.assertEqual(landed, PAGE1)

    def test_second_page_failure_returns_no_partial_rows_and_keeps_cache(self):
        scope = self.use_explicit_account()
        cache_store.begin_scan("old-good", mode="test", expected_lids=["123"],
                               requested_lids=["123"], tests_requested=False,
                               course_index_version="idx", scope=scope)
        cache_store.commit_course_scan(
            "old-good", lid="123", course="高数",
            homeworks=[{"hwtid": "old", "title": "旧作业",
                        "deadline": "2026-12-01 10:00"}],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version="idx", scope=scope)
        cache_store.update_scan("old-good", status="complete", finished_at=time.time())

        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: {"tables": [[HEADER, ["坏行", "坏日期"]]],
                    "row_cells": [[_cell("坏行"), _cell("坏日期")]],
                    "text": "共34 条记录", "anchors": []},
        }
        _page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete", why)
        self.assertTrue(any(item["title"] == "旧作业"
                            for item in cache_store.load_items(scope)))

    def test_loop_back_to_first_page_is_rejected(self):
        routes = {
            PAGE1: hw_page(range(1, 31), 60,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: hw_page(range(31, 61), 60,
                           next_href="hwtask.jsp?courseId=123"),
        }
        _page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("循环", why)

    def test_repeated_platform_ids_are_not_deduped_into_success(self):
        routes = {
            PAGE1: hw_page(range(1, 31), 60,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: hw_page(range(1, 31), 60),
        }
        _page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("重复平台 ID", why)

    def test_cross_domain_next_is_rejected_without_navigation(self):
        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="https://evil.example/hwtask.jsp?s_gotopage=2"),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("跨域", why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_javascript_next_is_rejected(self):
        routes = {
            PAGE1: hw_page(
                range(1, 31), 34,
                anchors=[{"text": "下一页", "href": "javascript:void(0)",
                          "onclick": "nextPage()", "disabled": False}]),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("下一页", why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_footer_total_mismatch_is_rejected(self):
        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: hw_page(range(31, 34), 34),
        }
        _page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("页脚总记录数 34", why)

    def test_valid_empty_requires_zero_record_evidence(self):
        no_evidence = HW_LIST + "?courseId=123&case=noevidence"
        routes = {
            PAGE1: hw_page([], 0, header_only=True, text="暂无作业 共 0 条记录"),
            no_evidence: hw_page([], 0, header_only=True, text=""),
        }
        _page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "empty", why)

        _page, rows, status, _why, _landed = self.collect(
            routes, start=no_evidence)
        self.assertEqual(rows, [])
        self.assertEqual(status, "invalid")

    def test_zero_records_with_residual_last_link_is_valid_empty(self):
        routes = {PAGE1: hw_page([], 0, header_only=True, text="共0条记录",
            anchors=[{"text": "尾页", "href": "hwtask.jsp?s_page=last"}])}
        page, rows, status, why, _ = self.collect(routes)
        self.assertEqual((rows, status), ([], "empty"), why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_zero_records_with_active_next_remains_incomplete(self):
        routes = {PAGE1: hw_page([], 0, header_only=True, text="共0条记录",
            next_href="hwtask.jsp?s_gotopage=2")}
        page, rows, status, why, _ = self.collect(routes)
        self.assertEqual((rows, status), ([], "incomplete"), why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_first_landing_with_wrong_course_id_is_rejected(self):
        wrong = HW_LIST + "?courseId=999"
        routes = {wrong: hw_page(range(1, 31), 34)}
        page, rows, status, why, _landed = self.collect(routes, start=wrong)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("其它课程 ID", why)
        self.assertEqual(page.gotos, [wrong])

    def test_first_landing_with_invalid_port_is_rejected(self):
        wrong = ("https://course.buct.edu.cn:99999/meol/common/hw/student/"
                 "hwtask.jsp?courseId=123")
        routes = {wrong: hw_page(range(1, 31), 34)}
        page, rows, status, why, _landed = self.collect(routes, start=wrong)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("端口非法", why)
        self.assertEqual(page.gotos, [wrong])

    def test_transfer_entry_may_land_on_a_different_path(self):
        routes = {
            TRANSFER: {"redirect": PAGE1},
            PAGE1: hw_page(range(1, 6), 5),
        }
        page, rows, status, why, landed = self.collect(routes, start=TRANSFER)
        self.assertEqual(status, "ok", why)
        self.assertEqual(len(rows), 5)
        self.assertEqual(landed, PAGE1)
        self.assertEqual(page.gotos, [TRANSFER])

    def test_first_landing_cross_scheme_from_transfer_is_rejected(self):
        downgraded = (HW_LIST.replace("https://", "http://")
                      + "?courseId=123")
        routes = {
            TRANSFER: {"redirect": downgraded},
            downgraded: hw_page(range(1, 6), 5),
        }
        page, rows, status, why, _landed = self.collect(routes, start=TRANSFER)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("跨源", why)
        self.assertEqual(page.gotos, [TRANSFER])

    def test_next_cross_scheme_port_or_invalid_port_is_rejected_without_navigation(self):
        next_hrefs = [
            "http://course.buct.edu.cn/meol/common/hw/student/hwtask.jsp?s_gotopage=2",
            "https://course.buct.edu.cn:8443/meol/common/hw/student/hwtask.jsp?s_gotopage=2",
            "https://course.buct.edu.cn:99999/meol/common/hw/student/hwtask.jsp?s_gotopage=2",
        ]
        for next_href in next_hrefs:
            with self.subTest(next_href=next_href):
                routes = {PAGE1: hw_page(range(1, 31), 34,
                                         next_href=next_href)}
                page, rows, status, why, _landed = self.collect(routes)
                self.assertEqual(rows, [])
                self.assertEqual(status, "incomplete")
                self.assertTrue("跨域" in why or "端口非法" in why, why)
                self.assertEqual(page.gotos, [PAGE1])

    def test_redirected_next_cross_origin_is_rejected(self):
        redirected = ("http://course.buct.edu.cn/meol/common/hw/student/"
                      "hwtask.jsp?s_gotopage=2")
        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: {"redirect": redirected},
            redirected: hw_page(range(31, 35), 34),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("跨域", why)
        self.assertEqual(page.gotos, [PAGE1, PAGE2])

    def test_terminal_link_without_total_or_last_page_evidence_is_incomplete(self):
        routes = {
            PAGE1: hw_page(range(1, 6), 0, text="[首页] [尾页]",
                           anchors=[{"text": "尾页",
                                     "href": "hwtask.jsp?s_page=last",
                                     "onclick": "", "disabled": False}]),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("尾页", why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_real_last_page_with_terminal_and_matching_total_is_ok(self):
        routes = {
            PAGE1: hw_page(range(1, 6), 8,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: hw_page(range(6, 9), 8,
                           anchors=[{"text": "尾页",
                                     "href": "hwtask.jsp?s_page=last",
                                     "onclick": "", "disabled": False}]),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(status, "ok", why)
        self.assertEqual(len(rows), 8)
        self.assertEqual(page.gotos, [PAGE1, PAGE2])

    def test_second_page_auth_is_reported_without_partial_rows(self):
        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: {"text": "统一身份认证登录", "tables": [],
                    "row_cells": [], "anchors": []},
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "auth", why)
        self.assertEqual(page.gotos, [PAGE1, PAGE2])

    def test_page_limit_rejects_partial_results(self):
        routes = {}
        for page_no in range(1, 52):
            url = PAGE1 if page_no == 1 else HW_LIST + f"?s_gotopage={page_no}"
            next_href = (f"hwtask.jsp?s_gotopage={page_no + 1}"
                         if page_no < 51 else None)
            routes[url] = hw_page([page_no], 51, next_href=next_href)
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("最大翻页数", why)
        self.assertEqual(len(page.gotos), scrape.MAX_LIST_PAGES)
        self.assertNotIn(HW_LIST + "?s_gotopage=51", page.gotos)

    def test_slow_dom_parse_exhausting_budget_is_incomplete(self):
        clock = [100.0]
        page = SlowParsePagedFakePage(
            {PAGE1: hw_page([1], 1)}, PAGE1, clock, advance=10.0)
        with patch.object(scrape.time, "monotonic",
                          side_effect=lambda: clock[0]):
            rows, status, why, _landed = scrape._collect_list_pages(
                page, "homework", PAGE1, "123", 105.0, False, "offline")
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("预算", why)

    def test_retry_uses_landed_list_url_not_transfer(self):
        page = FakePage(
            text="课程作业 截止时间",
            url=BASE + "/meol/lesson/enter_course.jsp?lid=123&t=hw")
        seen_sources = []

        def collect(_page, kind, source, lid, deadline, debug, prefix):
            seen_sources.append(source)
            if len(seen_sources) == 1:
                return [], "incomplete", "first page broken", PAGE1
            return ([{"hwtid": "1", "title": "A",
                      "deadline": "2026-12-01 10:00"}], "ok", "ok", PAGE1)

        with patch.object(scrape, "get_column_href", return_value=TRANSFER), \
                patch.object(scrape, "_collect_list_pages", side_effect=collect):
            rows, _tests, _notes, statuses = scrape.fetch_course_both(
                None, page, "123", "高数", want_tests=False,
                empty_retries=1, return_status=True)
        self.assertEqual(statuses["homework"], "ok")
        self.assertEqual(len(rows), 1)
        self.assertEqual(seen_sources, [TRANSFER, PAGE1])
        self.assertNotIn(TRANSFER, seen_sources[1:])
        self.assertFalse(scrape._retry_list_url_usable(TRANSFER))
        self.assertTrue(scrape._retry_list_url_usable(PAGE1))

    def test_cross_course_next_is_rejected(self):
        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="hwtask.jsp?courseId=999&s_gotopage=2"),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("其它课程 ID", why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_redirected_next_cannot_switch_course(self):
        redirected = HW_LIST + "?courseId=999&s_gotopage=2"
        routes = {
            PAGE1: hw_page(range(1, 31), 34,
                           next_href="hwtask.jsp?s_gotopage=2"),
            PAGE2: {"redirect": redirected},
            redirected: hw_page(range(31, 35), 34),
        }
        _page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("其它课程 ID", why)

    def test_numeric_page_links_do_not_override_explicit_next(self):
        routes = {
            PAGE1: hw_page(
                range(1, 31), 34,
                next_href="hwtask.jsp?s_gotopage=2",
                anchors=[{"text": "2", "href": "hwtask.jsp?s_gotopage=2",
                          "onclick": "", "disabled": False},
                         {"text": "3", "href": "hwtask.jsp?s_gotopage=3",
                          "onclick": "", "disabled": False}]),
            PAGE2: hw_page(range(31, 35), 34),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(status, "ok", why)
        self.assertEqual(len(rows), 34)
        self.assertEqual(page.gotos, [PAGE1, PAGE2])

    def test_expired_budget_fails_before_navigation(self):
        page = PagedFakePage({PAGE1: hw_page(range(1, 31), 34)}, PAGE1)
        rows, status, why, _landed = scrape._collect_list_pages(
            page, "homework", PAGE1, "123", time.monotonic() - 1,
            False, "offline")
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("预算", why)
        self.assertEqual(page.gotos, [])

    def test_test_list_pagination_uses_platform_test_id(self):
        test_list = BASE + "/meol/common/question/test/student/list.jsp?cateId=7"
        test_page2 = (BASE + "/meol/common/question/test/student/list.jsp"
                      + "?s_gotopage=2")
        header = ["标题", "开始时间", "截止时间", "允许次数", "限制用时",
                  "开始测试", "交卷", "查看结果"]

        def test_row(i):
            return [
                _cell(f"测试 {i}"),
                _cell("2026-11-01 09:00"),
                _cell("2026-12-01 10:00"),
                _cell("1"),
                _cell("60"),
                {"text": "开始测试", "href": None,
                 "onclick": f"return gotostart('{i}','client','new03')"},
                _cell("交卷"),
                _cell("查看结果", BASE + f"/meol/common/question/test/student/testanswer.jsp?testId={i}"),
            ]

        def test_spec(ids, total, next_href=None):
            row_cells = [test_row(i) for i in ids]
            table = [header] + [[cell["text"] for cell in row] for row in row_cells]
            anchors = []
            if next_href:
                anchors.append({"text": "下一页", "href": next_href,
                                "onclick": "", "disabled": False})
            return {"tables": [table], "row_cells": row_cells,
                    "text": f"共{total} 条记录", "anchors": anchors}

        routes = {
            test_list: test_spec(range(1, 31), 34, "list.jsp?s_gotopage=2"),
            test_page2: test_spec(range(31, 35), 34),
        }
        page, rows, status, why, _landed = self.collect(
            routes, start=test_list, kind="test")
        self.assertEqual(status, "ok", why)
        self.assertEqual(len(rows), 34)
        self.assertEqual(len({r["test_id"] for r in rows}), 34)
        self.assertEqual(page.gotos, [test_list, test_page2])

    def test_test_list_page_size_control_is_not_a_next_page(self):
        test_list = (BASE + "/meol/common/question/test/student/list.jsp"
                     + "?status=1&tagbug=client&sortDirection=-1&strStyle=new03"
                     + "&cateId=123&pagingPage=1&pagingNumberPer=20")
        header = ["标题", "开始时间", "截止时间", "允许次数", "限制用时",
                  "开始测试", "交卷", "查看结果"]
        setting = {
            "text": "设置", "href": "", "type": "button",
            "class": "classicLookItemsPerPageSubmitButton ItemsPerPageSubmitButton",
            "name": "", "id": "", "disabled": False,
            "onclick": ('window.location.href = "list.jsp?status=1&tagbug=client'
                        '&sortDirection=-1&strStyle=new03&cateId=123&pagingPage=1'
                        '&pagingNumberPer=" + document.getElementById('
                        '"pagingNumberPer").value;')}
        routes = {test_list: {
            "tables": [[header]], "row_cells": [],
            "text": "共0条记录 共1页", "anchors": [setting]}}

        page, rows, status, why, _landed = self.collect(
            routes, start=test_list, kind="test")
        self.assertEqual(rows, [])
        self.assertEqual(status, "empty", why)
        self.assertEqual(page.gotos, [test_list])

    def test_unknown_javascript_next_still_blocks_even_with_footer_total(self):
        routes = {
            PAGE1: hw_page(
                range(1, 31), 34,
                anchors=[{"text": "", "href": "", "onclick":
                          "window.location.href='hwtask.jsp?pagingPage=2'",
                          "disabled": False}]),
        }
        page, rows, status, why, _landed = self.collect(routes)
        self.assertEqual(rows, [])
        self.assertEqual(status, "incomplete")
        self.assertIn("JavaScript", why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_javascript_next_with_page_size_parameter_is_not_ignored(self):
        routes = {PAGE1: hw_page([1], 1, anchors=[{
            "text": "下一页", "href": "javascript:void(0)",
            "onclick": "goNext(pagingNumberPer)", "disabled": False}])}
        page, rows, status, why, _ = self.collect(routes)
        self.assertEqual((rows, status), ([], "incomplete"), why)
        self.assertEqual(page.gotos, [PAGE1])

    def test_test_list_pagingpage_numeric_next_is_followed(self):
        test_list = (BASE + "/meol/common/question/test/student/list.jsp"
                     + "?cateId=123&pagingPage=1&pagingNumberPer=20")
        test_page2 = (BASE + "/meol/common/question/test/student/list.jsp"
                      + "?cateId=123&pagingPage=2&pagingNumberPer=20")
        test_page3 = (BASE + "/meol/common/question/test/student/list.jsp"
                      + "?cateId=123&pagingPage=3&pagingNumberPer=20")
        header = ["标题", "开始时间", "截止时间", "允许次数", "限制用时",
                  "开始测试", "交卷", "查看结果"]

        def test_row(i):
            return [
                _cell(f"测试 {i}"), _cell("2026-11-01 09:00"),
                _cell("2026-12-01 10:00"), _cell("1"), _cell("60"),
                {"text": "开始测试", "href": None,
                 "onclick": f"return gotostart('{i}','client','new03')"},
                _cell("交卷"),
                _cell("查看结果", BASE + "/meol/common/question/test/student/"
                      + f"testanswer.jsp?testId={i}"),
            ]

        def test_spec(ids, total, anchors=None):
            row_cells = [test_row(i) for i in ids]
            return {"tables": [[header] + [[c["text"] for c in row]
                                           for row in row_cells]],
                    "row_cells": row_cells, "text": f"共{total}条记录 共2页",
                    "anchors": anchors or []}

        routes = {
            test_list: test_spec(
                range(1, 31), 34,
                [{"text": "2", "href": test_page2, "onclick": "",
                  "disabled": False},
                 {"text": "3", "href": test_page3, "onclick": "",
                  "disabled": False}]),
            test_page2: test_spec(range(31, 35), 34),
        }
        page, rows, status, why, _landed = self.collect(
            routes, start=test_list, kind="test")
        self.assertEqual(status, "ok", why)
        self.assertEqual(len(rows), 34)
        self.assertEqual(page.gotos, [test_list, test_page2])

    def test_tests_disabled_never_requests_test_column(self):
        page = FakePage(
            text="课程作业 截止时间",
            url=BASE + "/meol/lesson/enter_course.jsp?lid=123&t=hw")
        calls = []

        def column(_page, label, **kwargs):
            calls.append(label)
            return PAGE1

        with patch.object(scrape, "get_column_href", side_effect=column), \
                patch.object(scrape, "_collect_list_pages",
                             return_value=([{"hwtid": "1", "title": "A",
                                             "deadline": "2026-12-01 10:00"}],
                                           "ok", "ok", PAGE1)):
            rows, tests, _notes, statuses = scrape.fetch_course_both(
                None, page, "123", "高数", want_tests=False,
                empty_retries=0, return_status=True)
        self.assertEqual(calls, ["课程作业"])
        self.assertEqual(statuses["tests"], "disabled")
        self.assertEqual(len(rows), 1)
        self.assertEqual(tests, [])


if __name__ == "__main__":
    unittest.main()
