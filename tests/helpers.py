# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
if str(DATA) not in sys.path:
    sys.path.insert(0, str(DATA))

import cache_store  # noqa: E402


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="homework-test-")
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.env = {
            "HOMEWORK_CACHE_DB": str(self.root / "cache.sqlite3"),
            "HOMEWORK_CACHE_ASSETS": str(self.root / "assets"),
            "HOMEWORK_CONFIG_PATH": str(self.config_path),
            "HOMEWORK_LOCK_PATH": str(self.root / "platform.lock"),
        }
        self._env_patch = patch.dict(os.environ, self.env, clear=False)
        self._env_patch.start()
        for key in ("HOMEWORK_ACCOUNT_SCOPE", "HOMEWORK_ACCOUNT_CHANGE",
                    "HOMEWORK_CONTENT_LIMIT"):
            os.environ.pop(key, None)
        cache_store.init_db()

    def tearDown(self):
        self._env_patch.stop()
        self.tmp.cleanup()

    def write_config(self, data: dict):
        self.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def use_explicit_account(self, scope="test-account"):
        os.environ["HOMEWORK_ACCOUNT_SCOPE"] = scope
        return scope


class FakePage:
    def __init__(self, text="", url="https://course.buct.edu.cn/meol/lesson/enter_course.jsp?lid=11&t=hw",
                 tables=None, anchors=None, selectors=None, selector_values=None,
                 content_html="", body_by_prefix=None):
        self.text = text
        self.url = url
        self.tables = tables or []
        self.anchors = anchors or []
        self.selectors = selectors or {}
        self.selector_values = selector_values or {}
        self.content_html = content_html
        self.body_by_prefix = body_by_prefix or {}
        self.gotos = []

    def inner_text(self, selector, timeout=None):
        for prefix, text in self.body_by_prefix.items():
            if prefix in self.url:
                return text
        return self.text

    def eval_on_selector_all(self, selector, script):
        if selector == "table":
            return self.tables
        if selector == "a,button,input":
            return self.anchors
        if selector == "a":
            return self.anchors
        return []

    def eval_on_selector(self, selector, script):
        if selector in self.selector_values:
            return self.selector_values[selector]
        for key, value in self.selector_values.items():
            if key in selector:
                return value
        return None

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    def content(self):
        return self.content_html

    def close(self):
        pass


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [page]

    def new_page(self):
        return self.page

    def cookies(self, urls=None):
        return []
