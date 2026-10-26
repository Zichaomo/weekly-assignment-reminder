# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
if str(DATA) not in sys.path:
    sys.path.insert(0, str(DATA))

import cache_store  # noqa: E402


SCOPE = "migration-isolated"
ITEM_ONE = "platform:migration-isolated:1:homework:9001"
ITEM_TWO = "platform:migration-isolated:2:homework:9002"
ASSET_ID = "a" * 64
LEGACY_ASSET_PATH = "assets/legacy-original.png"
LEGACY_SOURCE_VERSION = "legacy-v1"
LEGACY_INDEX_VERSION = "legacy-index"


# Deliberately fixed v2 DDL.  It must not be derived from cache_store.init_db(),
# otherwise this regression would hide future migration defects.
V2_DDL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE scan_runs (
    scan_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL,
    platform TEXT NOT NULL, mode TEXT, course_index_version TEXT,
    parse_version TEXT, tests_requested INTEGER NOT NULL DEFAULT 0,
    expected_json TEXT NOT NULL DEFAULT '[]',
    requested_lids_json TEXT NOT NULL DEFAULT '[]',
    started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
    course_total INTEGER NOT NULL DEFAULT 0,
    course_done INTEGER NOT NULL DEFAULT 0,
    course_failed INTEGER NOT NULL DEFAULT 0,
    error TEXT, coverage_json TEXT
);
CREATE TABLE course_scan_state (
    account_scope TEXT NOT NULL, platform TEXT NOT NULL,
    lid TEXT NOT NULL, course TEXT NOT NULL, scan_id TEXT,
    checked_at REAL, homework_status TEXT, homework_checked_at REAL,
    homework_error TEXT, tests_status TEXT, tests_checked_at REAL,
    tests_error TEXT, homework_complete INTEGER NOT NULL DEFAULT 0,
    tests_complete INTEGER NOT NULL DEFAULT 0, index_version TEXT,
    parse_version TEXT, identity_generation INTEGER NOT NULL DEFAULT 0,
    notes_json TEXT, items_hash TEXT,
    PRIMARY KEY(account_scope, platform, lid)
);
CREATE TABLE assignments (
    account_scope TEXT NOT NULL, platform TEXT NOT NULL,
    lid TEXT NOT NULL, course TEXT NOT NULL, item_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL, platform_id TEXT,
    identity_uncertain INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '', deadline TEXT NOT NULL DEFAULT '',
    publisher TEXT, content_url TEXT, submit_url TEXT, result_url TEXT,
    start_url TEXT, submit_state TEXT, submitted INTEGER, note TEXT,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    last_scan_id TEXT, active INTEGER NOT NULL DEFAULT 1,
    extra_json TEXT
);
CREATE TABLE assignment_contents (
    item_key TEXT PRIMARY KEY, status TEXT NOT NULL, source_url TEXT,
    source_checked_at REAL, ttl_sec INTEGER NOT NULL, source_hash TEXT,
    source_version TEXT, revision INTEGER NOT NULL DEFAULT 0,
    raw_html TEXT, blocks_json TEXT, text TEXT, asset_ids_json TEXT,
    error TEXT, updated_at REAL NOT NULL,
    FOREIGN KEY(item_key) REFERENCES assignments(item_key)
);
CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, path TEXT NOT NULL,
    mime TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
    bytes INTEGER NOT NULL, source_url TEXT, checked_at REAL NOT NULL
);
CREATE TABLE content_views (
    item_key TEXT NOT NULL, source_version TEXT NOT NULL,
    view_key TEXT NOT NULL DEFAULT 'default',
    regions_json TEXT NOT NULL DEFAULT '[]',
    folded_json TEXT NOT NULL DEFAULT '[]', summary TEXT,
    review_status TEXT NOT NULL DEFAULT 'ok', updated_at REAL NOT NULL,
    PRIMARY KEY(item_key, source_version, view_key)
);
CREATE TABLE task_registry (
    task_id TEXT PRIMARY KEY, signature TEXT NOT NULL, op TEXT NOT NULL,
    params_json TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
    owner_pid INTEGER, result_summary TEXT
);
"""


class LegacyV2CacheMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="homework-migration-")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "cache.sqlite3"
        self.config_path = self.root / "config.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.env = {
            "HOMEWORK_CACHE_DB": str(self.db_path),
            "HOMEWORK_CACHE_ASSETS": str(self.root / "assets"),
            "HOMEWORK_CONFIG_PATH": str(self.config_path),
            "HOMEWORK_LOCK_PATH": str(self.root / "platform.lock"),
            "HOMEWORK_ACCOUNT_SCOPE": SCOPE,
        }
        self._env_patch = patch.dict(os.environ, self.env, clear=False)
        self._env_patch.start()
        os.environ.pop("HOMEWORK_ACCOUNT_CHANGE", None)
        self._seed_v2()

    def tearDown(self):
        self._env_patch.stop()
        self.tmp.cleanup()

    def _connect(self):
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _seed_v2(self):
        now = time.time() - 120
        identity_marker = hashlib.sha256("脱敏身份甲".encode("utf-8")).hexdigest()[:24]
        regions = [{
            "asset_id": ASSET_ID,
            "box": [0.1, 0.2, 0.8, 0.9],
            "origin": "manual",
        }]
        with closing(self._connect()) as con:
            con.executescript(V2_DDL)
            con.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                (("schema_version", "2"),
                 (f"identity:{SCOPE}", identity_marker)),
            )
            con.execute(
                """INSERT INTO scan_runs(scan_id,account_scope,platform,mode,
                   course_index_version,parse_version,tests_requested,
                   expected_json,requested_lids_json,started_at,finished_at,
                   status,course_total,course_done)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("legacy-scan", SCOPE, cache_store.PLATFORM, "test",
                 LEGACY_INDEX_VERSION, cache_store.PARSE_VERSION, 0,
                 json.dumps(["1", "2"]), json.dumps(["1", "2"]),
                 now, now + 1, "complete", 2, 2),
            )
            con.executemany(
                """INSERT INTO course_scan_state(account_scope,platform,lid,
                   course,scan_id,checked_at,homework_status,homework_checked_at,
                   tests_status,tests_checked_at,homework_complete,
                   tests_complete,index_version,parse_version,
                   identity_generation,notes_json,items_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (SCOPE, cache_store.PLATFORM, "1", "课程甲", "legacy-scan",
                     now, "ok", now, "disabled", now, 1, 1,
                     LEGACY_INDEX_VERSION, cache_store.PARSE_VERSION, 0, "[]", "h1"),
                    (SCOPE, cache_store.PLATFORM, "2", "课程乙", "legacy-scan",
                     now, "ok", now, "disabled", now, 1, 1,
                     LEGACY_INDEX_VERSION, cache_store.PARSE_VERSION, 0, "[]", "h2"),
                ),
            )
            con.executemany(
                """INSERT INTO assignments(account_scope,platform,lid,course,
                   item_key,kind,platform_id,identity_uncertain,title,deadline,
                   first_seen,last_seen,last_scan_id,active,extra_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (SCOPE, cache_store.PLATFORM, "1", "课程甲", ITEM_ONE,
                     "homework", "9001", 0, "作业甲", "2099-01-02 10:00",
                     now, now, "legacy-scan", 1, "{}"),
                    (SCOPE, cache_store.PLATFORM, "2", "课程乙", ITEM_TWO,
                     "homework", "9002", 0, "作业乙", "2099-01-03 10:00",
                     now, now, "legacy-scan", 1, "{}"),
                ),
            )
            con.execute(
                """INSERT INTO assignment_contents(item_key,status,source_url,
                   source_checked_at,ttl_sec,source_hash,source_version,revision,
                   raw_html,blocks_json,text,asset_ids_json,error,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ITEM_ONE, "ready", "https://course.invalid/legacy",
                 now, 21600, "legacy-hash", LEGACY_SOURCE_VERSION, 1,
                 "<p>脱敏正文甲</p>",
                 json.dumps([{"id": "p1", "type": "paragraph",
                              "text": "脱敏正文甲"}], ensure_ascii=False),
                 "脱敏正文甲", json.dumps([ASSET_ID]), None, now),
            )
            con.execute(
                """INSERT INTO assets(asset_id,sha256,path,mime,width,height,
                   bytes,source_url,checked_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (ASSET_ID, "b" * 64, LEGACY_ASSET_PATH, "image/png",
                 640, 480, 1024, "https://course.invalid/legacy.png", now),
            )
            con.execute(
                """INSERT INTO content_views(item_key,source_version,view_key,
                   regions_json,folded_json,summary,review_status,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (ITEM_ONE, LEGACY_SOURCE_VERSION, "default",
                 json.dumps(regions, ensure_ascii=False),
                 json.dumps(["p1"]), "人工保留摘要", "ok", now),
            )
            con.execute(
                """INSERT INTO task_registry(task_id,signature,op,params_json,
                   status,created_at,started_at,finished_at,owner_pid,
                   result_summary) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("legacy-task", "legacy-signature", "fetch", "{}", "done",
                 now, now, now + 1, 4242, "legacy done"),
            )
            con.commit()

    def _columns(self, table):
        with closing(self._connect()) as con:
            return {str(row["name"])
                    for row in con.execute(f"PRAGMA table_info({table})")}

    def _meta(self, key):
        with closing(self._connect()) as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _count(self, table):
        with closing(self._connect()) as con:
            return int(con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def _row(self, sql, params=()):
        with closing(self._connect()) as con:
            row = con.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _rows(self, sql, params=()):
        with closing(self._connect()) as con:
            return [dict(row) for row in con.execute(sql, params).fetchall()]

    def _migrate_twice(self):
        # Prove the fixture is genuinely pre-migration before touching init_db().
        self.assertEqual(self._meta("schema_version"), "2")
        self.assertNotIn("identity_generation", self._columns("scan_runs"))
        self.assertNotIn("identity_generation", self._columns("assignment_contents"))
        self.assertNotIn("original_path", self._columns("assets"))
        self.assertNotIn("scan_id", self._columns("task_registry"))

        cache_store.init_db()
        first_columns = {
            table: self._columns(table)
            for table in ("scan_runs", "assignment_contents", "assets", "task_registry")
        }
        cache_store.init_db()
        second_columns = {
            table: self._columns(table)
            for table in ("scan_runs", "assignment_contents", "assets", "task_registry")
        }
        self.assertEqual(first_columns, second_columns)

    def test_v2_migration_is_idempotent_and_preserves_legacy_rows(self):
        self._migrate_twice()

        self.assertEqual(self._meta("schema_version"), "3")
        self.assertLessEqual({"identity_generation"}, self._columns("scan_runs"))
        self.assertLessEqual({"identity_generation"},
                             self._columns("assignment_contents"))
        self.assertLessEqual(
            {"original_path", "display_path", "orientation_corrected", "crop_safe"},
            self._columns("assets"),
        )
        self.assertLessEqual({"scan_id", "owner_started"},
                             self._columns("task_registry"))

        self.assertEqual(self._count("scan_runs"), 1)
        self.assertEqual(self._count("course_scan_state"), 2)
        self.assertEqual(self._count("assignments"), 2)
        self.assertEqual(self._count("assignment_contents"), 1)
        self.assertEqual(self._count("assets"), 1)
        self.assertEqual(self._count("content_views"), 1)
        self.assertEqual(self._count("task_registry"), 1)

        assignments = {
            row["item_key"]: row for row in self._rows(
                "SELECT item_key,lid,course,title,platform_id,active FROM assignments")
        }
        self.assertEqual(assignments[ITEM_ONE]["course"], "课程甲")
        self.assertEqual(assignments[ITEM_ONE]["title"], "作业甲")
        self.assertEqual(assignments[ITEM_TWO]["course"], "课程乙")
        self.assertEqual(assignments[ITEM_TWO]["title"], "作业乙")
        self.assertTrue(assignments[ITEM_ONE]["active"])
        self.assertTrue(assignments[ITEM_TWO]["active"])

        content = self._row(
            """SELECT status,source_version,text,blocks_json,asset_ids_json,
                      identity_generation FROM assignment_contents WHERE item_key=?""",
            (ITEM_ONE,),
        )
        self.assertEqual(content["status"], "ready")
        self.assertEqual(content["source_version"], LEGACY_SOURCE_VERSION)
        self.assertEqual(content["text"], "脱敏正文甲")
        self.assertEqual(json.loads(content["blocks_json"])[0]["text"], "脱敏正文甲")
        self.assertEqual(json.loads(content["asset_ids_json"]), [ASSET_ID])
        self.assertEqual(content["identity_generation"], 0)

        asset = self._row("SELECT * FROM assets WHERE asset_id=?", (ASSET_ID,))
        self.assertEqual(asset["path"], LEGACY_ASSET_PATH)
        self.assertEqual(asset["original_path"], LEGACY_ASSET_PATH)
        self.assertEqual(asset["display_path"], LEGACY_ASSET_PATH)
        self.assertEqual(asset["orientation_corrected"], 0)
        self.assertEqual(asset["crop_safe"], 1)

        task = self._row("SELECT * FROM task_registry WHERE task_id='legacy-task'")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["result_summary"], "legacy done")
        self.assertIsNone(task["scan_id"])
        self.assertIsNone(task["owner_started"])

        view = self._row(
            "SELECT * FROM content_views WHERE item_key=?", (ITEM_ONE,))
        self.assertEqual(view["source_version"], LEGACY_SOURCE_VERSION)
        self.assertEqual(json.loads(view["folded_json"]), ["p1"])
        self.assertEqual(view["summary"], "人工保留摘要")

        items = {item["item_key"]: item for item in cache_store.load_items(SCOPE)}
        self.assertTrue(items[ITEM_ONE]["content_ready"])
        self.assertEqual(items[ITEM_ONE]["content_text"], "脱敏正文甲")
        self.assertEqual(items[ITEM_ONE]["view"]["summary"], "人工保留摘要")

        cov = cache_store.coverage(
            SCOPE, expected_lids=["1", "2"], tests_requested=False,
            current_index_version=LEGACY_INDEX_VERSION)
        self.assertTrue(cov["publishable"], cov["publish_block_reasons"])

    def test_identity_change_hides_legacy_body_and_single_course_cannot_revive_peer(self):
        self._migrate_twice()

        same = cache_store.bind_identity("脱敏身份甲", scope=SCOPE)
        self.assertFalse(same["changed"])
        changed = cache_store.bind_identity("脱敏身份乙", scope=SCOPE, allow_change=True)
        self.assertTrue(changed["changed"])

        item = cache_store.get_item(ITEM_ONE, SCOPE)
        self.assertTrue(item["content_identity_stale"])
        self.assertEqual(item["content_status"], "identity_changed")
        self.assertFalse(item["content_ready"])
        self.assertFalse(item["content_has_blocks"])
        self.assertEqual(item["content_text"], "")
        self.assertEqual(item["content_blocks"], [])
        self.assertIsNone(item["source_version"])
        self.assertEqual(item["stale_view"]["source_version"], LEGACY_SOURCE_VERSION)
        self.assertEqual(item["stale_view"]["review_status"], "needs_review")

        exported = cache_store.export_views(SCOPE)
        view = next(row for row in exported["views"] if row["item_key"] == ITEM_ONE)
        self.assertEqual(view["source_version"], LEGACY_SOURCE_VERSION)
        self.assertEqual(view["review_status"], "needs_review")
        self.assertEqual(view["summary"], "人工保留摘要")
        self.assertEqual(view["folded_block_ids"], ["p1"])
        self.assertEqual(view["regions"][0]["asset_id"], ASSET_ID)

        cache_store.begin_scan(
            "legacy-refresh-one", mode="test", expected_lids=["1"],
            requested_lids=["1"], tests_requested=False,
            course_index_version=LEGACY_INDEX_VERSION, scope=SCOPE)
        cache_store.commit_course_scan(
            "legacy-refresh-one", lid="1", course="课程甲",
            homeworks=[{"hwtid": "9001", "title": "作业甲",
                        "deadline": "2099-01-02 10:00"}],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version=LEGACY_INDEX_VERSION, scope=SCOPE)
        cache_store.update_scan(
            "legacy-refresh-one", status="complete", finished_at=time.time())

        cov = cache_store.coverage(
            SCOPE, expected_lids=["1", "2"], tests_requested=False,
            current_index_version=LEGACY_INDEX_VERSION)
        self.assertFalse(cov["publishable"])
        self.assertIn("2", cov["failed_courses"])
        self.assertNotIn("1", cov["failed_courses"])

        states = {
            row["lid"]: row for row in self._rows(
                "SELECT * FROM course_scan_state WHERE account_scope=? AND platform=?",
                (SCOPE, cache_store.PLATFORM))
        }
        self.assertEqual(states["1"]["homework_status"], "ok")
        self.assertEqual(states["2"]["homework_status"], "invalid")
        self.assertIsNone(states["2"]["scan_id"])


if __name__ == "__main__":
    unittest.main()