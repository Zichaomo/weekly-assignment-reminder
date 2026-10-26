# -*- coding: utf-8 -*-
"""SQLite cache for complete homework/test lists and assignment content."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
PLATFORM = "buct-theol"
PARSE_VERSION = "2026-10-25.1"
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def _load_config() -> dict:
    path = Path(os.environ.get("HOMEWORK_CONFIG_PATH") or (ROOT / "config.json"))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def cfg() -> dict:
    return _load_config()


def cache_settings() -> dict:
    c = cfg()
    cache = c.get("cache") if isinstance(c.get("cache"), dict) else {}
    content = c.get("content") if isinstance(c.get("content"), dict) else {}
    scan = c.get("scan") if isinstance(c.get("scan"), dict) else {}
    return {
        "db_path": Path(os.environ.get("HOMEWORK_CACHE_DB")
                        or cache.get("db_path")
                        or (ROOT / "cache" / "homework.sqlite3")),
        "assets_dir": Path(os.environ.get("HOMEWORK_CACHE_ASSETS")
                           or cache.get("assets_dir")
                           or (ROOT / "cache" / "assets")),
        "publish_fresh_sec": int(os.environ.get("HOMEWORK_PUBLISH_FRESH_SEC")
                                 or scan.get("publish_fresh_sec", 300)),
        "content_ttl_sec": int(os.environ.get("HOMEWORK_CONTENT_TTL")
                               or content.get("ttl_seconds", 6 * 3600)),
        "content_window_days": int(os.environ.get("HOMEWORK_CONTENT_WINDOW_DAYS")
                                   or content.get("window_days", 14)),
        # 0 means "all due items"; a non-zero value is an explicit per-task cap,
        # never a silent truncation of the visible window.
        "content_limit": int(os.environ.get("HOMEWORK_CONTENT_LIMIT")
                             or content.get("batch_limit", 0)),
        "course_gap_sec": float(os.environ.get("HOMEWORK_COURSE_GAP_SEC")
                                or scan.get("course_gap_sec", 0.3)),
        "no_tests_review_days": int(scan.get("no_tests_review_days", 7)),
        "asset_max_bytes": int(content.get("asset_max_bytes", 8 * 1024 * 1024)),
        "asset_max_pixels": int(content.get("asset_max_pixels", 40_000_000)),
        "step_timeout_sec": float(scan.get("step_timeout_sec", 12)),
        "course_timeout_sec": float(scan.get("course_timeout_sec", 90)),
    }


class AccountIdentityConflict(RuntimeError):
    pass


def configured_account_scope() -> str | None:
    raw = (os.environ.get("HOMEWORK_ACCOUNT_SCOPE")
           or cfg().get("account_scope") or "").strip()
    return raw if raw and _SCOPE_RE.match(raw) else None


def account_scope() -> str:
    """Stable non-credential namespace; never derived from session cookies."""
    explicit = configured_account_scope()
    if explicit:
        return explicit
    local = (os.environ.get("USERPROFILE") or os.environ.get("HOME")
             or os.environ.get("USERNAME") or "local")
    seed = f"{PLATFORM}|{local}|{ROOT / 'storage_state.json'}"
    return "local-unverified-" + hashlib.sha256(
        seed.encode("utf-8", "replace")).hexdigest()[:16]


def _identity_key(scope: str) -> str:
    return f"identity:{scope}"


def _identity_generation_key(scope: str) -> str:
    return f"identity_generation:{scope}"


def _scope_identity_generation(scope: str,
                               con: sqlite3.Connection | None = None) -> int:
    """Return the account/authentication generation for a cache scope."""
    if con is None:
        with connection() as own:
            return _scope_identity_generation(scope, own)
    row = con.execute("SELECT value FROM meta WHERE key=?",
                      (_identity_generation_key(scope),)).fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _advance_identity_generation(con: sqlite3.Connection, scope: str) -> int:
    generation = _scope_identity_generation(scope, con) + 1
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                (_identity_generation_key(scope), str(generation)))
    return generation


_IDENTITY_STALE_REASON = "平台账号身份或认证状态已变更；旧正文只可离线导出，需重新抓取并复核"


def _invalidate_scope_state(con: sqlite3.Connection, scope: str, reason: str,
                            status: str, *, bump_generation: bool) -> int:
    """Invalidate old scan coverage and, when account certainty changed, bodies."""
    now = time.time()
    if bump_generation:
        _advance_identity_generation(con, scope)
        con.execute(
            """UPDATE assignment_contents SET status='identity_changed', error=?
               WHERE item_key IN (
                 SELECT item_key FROM assignments
                 WHERE account_scope=? AND platform=?)""",
            (_IDENTITY_STALE_REASON, scope, PLATFORM))
        con.execute(
            """UPDATE content_views SET review_status='needs_review'
               WHERE item_key IN (
                 SELECT item_key FROM assignments
                 WHERE account_scope=? AND platform=?)""",
            (scope, PLATFORM))
    con.execute(
        """UPDATE course_scan_state SET scan_id=NULL, checked_at=NULL,
           homework_status='invalid', homework_checked_at=NULL, homework_error=?,
           tests_status='invalid', tests_checked_at=NULL, tests_error=?,
           homework_complete=0, tests_complete=0, notes_json='[]', items_hash=NULL
           WHERE account_scope=? AND platform=?""",
        (reason[:500], reason[:500], scope, PLATFORM))
    cur = con.execute(
        """UPDATE scan_runs SET status=?, finished_at=COALESCE(finished_at,?),
           error=? WHERE account_scope=? AND platform=?
           AND status IN ('running','complete','partial','error')""",
        (status, now, reason[:500], scope, PLATFORM))
    return int(cur.rowcount or 0)


def bind_identity(marker: str, scope: str | None = None,
                  allow_change: bool | None = None) -> dict:
    """Bind a non-credential visible identity marker to the cache scope.

    A mismatch is never silently accepted: the caller must either switch
    ``account_scope`` or explicitly set ``HOMEWORK_ACCOUNT_CHANGE=1`` after
    understanding that it changes accounts.
    """
    init_db()
    scope = scope or account_scope()
    marker = (marker or "").strip()
    if not marker:
        return identity_status(scope)
    digest = hashlib.sha256(marker.encode("utf-8", "replace")).hexdigest()[:24]
    if allow_change is None:
        allow_change = os.environ.get("HOMEWORK_ACCOUNT_CHANGE") == "1"
    with connection(write=True) as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (_identity_key(scope),)).fetchone()
        old = row["value"] if row else None
        changed = bool(old and old != digest)
        if changed and not allow_change:
            raise AccountIdentityConflict(
                "检测到平台账号与本地缓存身份不一致。为避免串账号复用，"
                "请切换 config.json 的 account_scope 后重新扫描；如确认要更换当前范围，"
                "先备份再设置 HOMEWORK_ACCOUNT_CHANGE=1 执行一次。")
        if changed:
            # Old snapshots belong to another account.  Keep them readable
            # offline, but no old scan or course coverage may be reused.
            now = time.time()
            reason = "平台账号身份已变更；旧缓存只可离线查看，禁止发布"
            _invalidate_scope_state(con, scope, reason, "identity_changed",
                                    bump_generation=True)
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        (f"identity_previous:{scope}", old))
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        (f"identity_changed_at:{scope}", str(now)))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    (_identity_key(scope), digest))
    status = identity_status(scope)
    status["changed"] = changed
    return status


def invalidate_scope_scans(reason: str, scope: str | None = None,
                           status: str = "identity_changed") -> int:
    """Make a prior successful scan unusable after auth/identity failure."""
    init_db()
    scope = scope or account_scope()
    with connection(write=True) as con:
        return _invalidate_scope_state(con, scope, reason, status,
                                       bump_generation=True)


def identity_status(scope: str | None = None) -> dict:
    init_db()
    scope = scope or account_scope()
    explicit = configured_account_scope() is not None
    with connection() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (_identity_key(scope),)).fetchone()
    marker = row["value"] if row else None
    verified = bool(explicit or marker)
    return {"scope": scope, "verified": verified,
            "source": "config" if explicit else ("platform-marker" if marker else "unverified"),
            "marker_hash": marker,
            "reason": ("使用 config.json/env 中显式 account_scope" if explicit
                       else ("已绑定平台可见身份标记" if marker
                             else "默认路径哈希无法确认平台账号；仅允许离线查看，正式发布被阻断"))}


def version_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _connect() -> sqlite3.Connection:
    s = cache_settings()
    s["db_path"].parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(s["db_path"]), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def connection(write: bool = False):
    con = _connect()
    try:
        if write:
            con.execute("BEGIN IMMEDIATE")
        yield con
        if write:
            con.commit()
    except Exception:
        if write:
            con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with connection(write=True) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scan_runs (
            scan_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL,
            platform TEXT NOT NULL, mode TEXT, course_index_version TEXT,
            parse_version TEXT, identity_generation INTEGER NOT NULL DEFAULT 0,
            tests_requested INTEGER NOT NULL DEFAULT 0,
            expected_json TEXT NOT NULL DEFAULT '[]',
            requested_lids_json TEXT NOT NULL DEFAULT '[]',
            started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
            course_total INTEGER NOT NULL DEFAULT 0,
            course_done INTEGER NOT NULL DEFAULT 0,
            course_failed INTEGER NOT NULL DEFAULT 0,
            error TEXT, coverage_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scan_scope_time
            ON scan_runs(account_scope, platform, started_at DESC);
        CREATE TABLE IF NOT EXISTS course_scan_state (
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
        CREATE TABLE IF NOT EXISTS assignments (
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
        CREATE INDEX IF NOT EXISTS idx_assign_scope_active
            ON assignments(account_scope, platform, active, lid, kind);
        CREATE TABLE IF NOT EXISTS assignment_contents (
            item_key TEXT PRIMARY KEY, status TEXT NOT NULL, source_url TEXT,
            source_checked_at REAL, ttl_sec INTEGER NOT NULL, source_hash TEXT,
            source_version TEXT, identity_generation INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0,
            raw_html TEXT, blocks_json TEXT, text TEXT, asset_ids_json TEXT,
            error TEXT, updated_at REAL NOT NULL,
            FOREIGN KEY(item_key) REFERENCES assignments(item_key)
        );
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, path TEXT NOT NULL,
            mime TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            bytes INTEGER NOT NULL, source_url TEXT, checked_at REAL NOT NULL,
            original_path TEXT, display_path TEXT,
            orientation_corrected INTEGER NOT NULL DEFAULT 0,
            crop_safe INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS content_views (
            item_key TEXT NOT NULL, source_version TEXT NOT NULL,
            view_key TEXT NOT NULL DEFAULT 'default',
            regions_json TEXT NOT NULL DEFAULT '[]',
            folded_json TEXT NOT NULL DEFAULT '[]', summary TEXT,
            review_status TEXT NOT NULL DEFAULT 'ok', updated_at REAL NOT NULL,
            PRIMARY KEY(item_key, source_version, view_key)
        );
        CREATE TABLE IF NOT EXISTS task_registry (
            task_id TEXT PRIMARY KEY, signature TEXT NOT NULL, op TEXT NOT NULL,
            params_json TEXT NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, started_at REAL, finished_at REAL,
            owner_pid INTEGER, result_summary TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_task_signature
            ON task_registry(signature, status, created_at DESC);
        """)
        columns = {str(r["name"]) for r in con.execute("PRAGMA table_info(task_registry)")}
        if "scan_id" not in columns:
            con.execute("ALTER TABLE task_registry ADD COLUMN scan_id TEXT")
        if "owner_started" not in columns:
            con.execute("ALTER TABLE task_registry ADD COLUMN owner_started INTEGER")
        for table in ("scan_runs", "course_scan_state", "assignment_contents"):
            table_columns = {str(r["name"])
                             for r in con.execute(f"PRAGMA table_info({table})")}
            if "identity_generation" not in table_columns:
                con.execute(
                    f"ALTER TABLE {table} ADD COLUMN identity_generation "
                    "INTEGER NOT NULL DEFAULT 0")
        asset_columns = {str(r["name"]) for r in con.execute("PRAGMA table_info(assets)")}
        for name, decl in (("original_path", "TEXT"), ("display_path", "TEXT"),
                           ("orientation_corrected", "INTEGER NOT NULL DEFAULT 0"),
                           ("crop_safe", "INTEGER NOT NULL DEFAULT 1")):
            if name not in asset_columns:
                con.execute(f"ALTER TABLE assets ADD COLUMN {name} {decl}")
        con.execute("UPDATE assets SET original_path=COALESCE(original_path,path), "
                    "display_path=COALESCE(display_path,path) WHERE display_path IS NULL")
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','3')")


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001
        return default


def begin_scan(scan_id: str, *, mode: str, expected_lids: Iterable[str],
               requested_lids: Iterable[str] | None = None, tests_requested: bool,
               course_index_version: str, parse_version: str = PARSE_VERSION,
               scope: str | None = None) -> str:
    init_db()
    scope = scope or account_scope()
    expected = sorted({str(x) for x in expected_lids})
    requested = sorted({str(x) for x in (requested_lids or expected)})
    now = time.time()
    with connection(write=True) as con:
        generation = _scope_identity_generation(scope, con)
        con.execute(
            """INSERT INTO scan_runs(scan_id,account_scope,platform,mode,
               course_index_version,parse_version,identity_generation,
               tests_requested,expected_json,requested_lids_json,started_at,
               status,course_total)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_id, scope, PLATFORM, mode, course_index_version, parse_version,
             generation, 1 if tests_requested else 0,
             json.dumps(expected, ensure_ascii=False),
             json.dumps(requested, ensure_ascii=False), now, "running", len(expected)),
        )
    return scan_id


def update_scan(scan_id: str, **fields: Any) -> None:
    allowed = {"status", "finished_at", "course_done", "course_failed", "error",
               "coverage_json", "course_total"}
    sets, vals = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        vals.append(json.dumps(value, ensure_ascii=False) if key == "coverage_json" else value)
    if not sets:
        return
    with connection(write=True) as con:
        row = con.execute("SELECT status FROM scan_runs WHERE scan_id=?",
                          (scan_id,)).fetchone()
        stale_statuses = {"identity_changed", "auth_failed", "invalidated"}
        if (row and row["status"] in stale_statuses
                and fields.get("status") in {"running", "complete", "partial"}):
            return
        con.execute(f"UPDATE scan_runs SET {','.join(sets)} WHERE scan_id=?", (*vals, scan_id))


def scan_progress(scan_id: str | None = None, scope: str | None = None) -> dict:
    init_db()
    scope = scope or account_scope()
    with connection() as con:
        if scan_id:
            row = con.execute("SELECT * FROM scan_runs WHERE scan_id=?", (scan_id,)).fetchone()
        else:
            row = con.execute(
                """SELECT * FROM scan_runs WHERE account_scope=? AND platform=?
                   ORDER BY started_at DESC LIMIT 1""", (scope, PLATFORM)).fetchone()
        if not row:
            return {"ok": False, "status": "none", "done": 0, "total": 0}
        states = con.execute(
            """SELECT lid,course,homework_status,tests_status,checked_at,
                      homework_error,tests_error
               FROM course_scan_state WHERE account_scope=? AND platform=?
                 AND scan_id=?""",
            (scope, PLATFORM, row["scan_id"])).fetchall()
    expected = _json(row["expected_json"], [])
    by_lid = {str(x["lid"]): x for x in states}
    completed, failed = [], []
    tests_requested = bool(row["tests_requested"])
    for lid in expected:
        st = by_lid.get(lid)
        if not st:
            continue
        hw_ok = st["homework_status"] in ("ok", "empty")
        tests_ok = (not tests_requested
                    or st["tests_status"] in ("ok", "empty", "no_entry", "blocked"))
        if hw_ok and tests_ok:
            completed.append(lid)
        elif st["homework_status"] in ("error", "invalid", "incomplete", "auth") \
                or (tests_requested and st["tests_status"] in
                    ("error", "invalid", "incomplete", "auth")):
            failed.append(lid)
    return {
        "ok": True, "scan_id": row["scan_id"], "status": row["status"],
        "mode": row["mode"], "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]), "done": len(completed),
        "failed": len(failed), "total": int(row["course_total"] or len(expected)),
        "completed_courses": [by_lid[x]["course"] for x in completed],
        "failed_courses": [by_lid[x]["course"] for x in failed],
        "tests_requested": bool(row["tests_requested"]),
        "course_index_version": row["course_index_version"],
        "parse_version": row["parse_version"], "error": row["error"],
    }


def _assignment_key(scope: str, lid: str, kind: str, platform_id: str | None,
                    title: str, deadline: str, ordinal: int) -> tuple[str, bool]:
    if platform_id:
        return f"platform:{scope}:{lid}:{kind}:{platform_id}", False
    seed = f"{title}|{deadline}|{ordinal}"
    ident = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"platform:{scope}:{lid}:{kind}:unknown:{ident}", True


def _upsert_assignments(con: sqlite3.Connection, scope: str, scan_id: str,
                        lid: str, course: str, kind: str, rows: list[dict],
                        now: float, complete: bool) -> tuple[list[str], str]:
    keys: list[str] = []
    for ordinal, item in enumerate(rows):
        platform_id = (item.get("hwtid") if kind == "homework"
                       else item.get("test_id"))
        platform_id = str(platform_id) if platform_id else None
        key, uncertain = _assignment_key(
            scope, lid, kind, platform_id, str(item.get("title") or ""),
            str(item.get("deadline") or ""), ordinal)
        keys.append(key)
        extra = {k: v for k, v in item.items() if k not in {
            "title", "deadline", "publisher", "content_url", "submit_url",
            "result_url", "start_url", "submit_state", "submitted", "note",
            "hwtid", "test_id", "course", "lid", "kind"}}
        con.execute(
            """INSERT INTO assignments(account_scope,platform,lid,course,item_key,
               kind,platform_id,identity_uncertain,title,deadline,publisher,
               content_url,submit_url,result_url,start_url,submit_state,submitted,
               note,first_seen,last_seen,last_scan_id,active,extra_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_key) DO UPDATE SET
                 course=excluded.course,title=excluded.title,deadline=excluded.deadline,
                 publisher=excluded.publisher,content_url=excluded.content_url,
                 submit_url=excluded.submit_url,result_url=excluded.result_url,
                 start_url=excluded.start_url,submit_state=excluded.submit_state,
                 submitted=excluded.submitted,note=excluded.note,
                 last_seen=excluded.last_seen,last_scan_id=excluded.last_scan_id,
                 active=1,extra_json=excluded.extra_json""",
            (scope, PLATFORM, lid, course, key, kind, platform_id,
             1 if uncertain else 0, str(item.get("title") or ""),
             str(item.get("deadline") or ""), item.get("publisher"),
             item.get("content_url"), item.get("submit_url"), item.get("result_url"),
             item.get("start_url"), item.get("submit_state"),
             (1 if item.get("submitted") is True else
              0 if item.get("submitted") is False else None),
             item.get("note"), now, now, scan_id, 1,
             json.dumps(extra, ensure_ascii=False)),
        )
    if complete:
        if keys:
            marks = ",".join("?" for _ in keys)
            con.execute(
                f"""UPDATE assignments SET active=0
                    WHERE account_scope=? AND platform=? AND lid=? AND kind=?
                      AND item_key NOT IN ({marks})""",
                (scope, PLATFORM, lid, kind, *keys))
        else:
            con.execute(
                """UPDATE assignments SET active=0
                   WHERE account_scope=? AND platform=? AND lid=? AND kind=?""",
                (scope, PLATFORM, lid, kind))
    return keys, version_hash(keys)


def commit_course_scan(scan_id: str, *, lid: str, course: str,
                       homeworks: list[dict] | None, tests: list[dict] | None,
                       homework_status: str, tests_status: str,
                       homework_error: str | None = None,
                       tests_error: str | None = None,
                       notes: list[str] | None = None,
                       index_version: str = "", parse_version: str = PARSE_VERSION,
                       scope: str | None = None, checked_at: float | None = None) -> dict:
    """Atomically replace one course's successfully checked columns."""
    init_db()
    scope = scope or account_scope()
    now = checked_at or time.time()
    hw_complete = homework_status in ("ok", "empty")
    # blocked is an explicit user choice (test blacklist); disabled means tests
    # were deliberately outside this scan's scope.  Both are complete *for that
    # scope*, while coverage() still blocks a later request that expands it.
    test_complete = tests_status in ("ok", "empty", "no_entry", "blocked", "disabled")
    keys_hw, hash_hw = ([], "")
    keys_tests, hash_tests = ([], "")
    with connection(write=True) as con:
        run = con.execute(
            """SELECT account_scope,status,identity_generation,tests_requested
               FROM scan_runs WHERE scan_id=?""", (scan_id,)).fetchone()
        current_generation = _scope_identity_generation(scope, con)
        if (not run or run["account_scope"] != scope or run["status"] != "running"
                or int(run["identity_generation"] or 0) != current_generation):
            return {"ignored": True, "homework_keys": [], "test_keys": [],
                    "homework_complete": False, "tests_complete": False}
        if hw_complete:
            keys_hw, hash_hw = _upsert_assignments(
                con, scope, scan_id, lid, course, "homework", homeworks or [], now, True)
        if test_complete:
            keys_tests, hash_tests = _upsert_assignments(
                con, scope, scan_id, lid, course, "test", tests or [], now, True)
        items_hash = version_hash({"homework": keys_hw, "tests": keys_tests})
        con.execute(
            """INSERT INTO course_scan_state(account_scope,platform,lid,course,
               scan_id,checked_at,homework_status,homework_checked_at,homework_error,
               tests_status,tests_checked_at,tests_error,homework_complete,
               tests_complete,index_version,parse_version,identity_generation,
               notes_json,items_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_scope,platform,lid) DO UPDATE SET
                 course=excluded.course,scan_id=excluded.scan_id,
                 checked_at=excluded.checked_at,homework_status=excluded.homework_status,
                 homework_checked_at=excluded.homework_checked_at,
                 homework_error=excluded.homework_error,tests_status=excluded.tests_status,
                 tests_checked_at=excluded.tests_checked_at,tests_error=excluded.tests_error,
                 homework_complete=excluded.homework_complete,
                 tests_complete=excluded.tests_complete,index_version=excluded.index_version,
                 parse_version=excluded.parse_version,
                 identity_generation=excluded.identity_generation,
                 notes_json=excluded.notes_json,items_hash=excluded.items_hash""",
            (scope, PLATFORM, lid, course, scan_id, now,
             homework_status, now if hw_complete else None, homework_error,
             tests_status, now if test_complete else None, tests_error,
             1 if hw_complete else 0, 1 if test_complete else 0,
             index_version, parse_version, current_generation,
             json.dumps(notes or [], ensure_ascii=False), items_hash),
        )
        tests_requested = bool(run["tests_requested"])
        row = con.execute(
            """SELECT SUM(CASE WHEN homework_status IN ('ok','empty')
                                 AND (?=0 OR tests_status IN
                                     ('ok','empty','no_entry','blocked'))
                               THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN homework_status NOT IN ('ok','empty')
                                 OR (?=1 AND tests_status IN
                                     ('error','invalid','incomplete','auth'))
                               THEN 1 ELSE 0 END) AS failed
               FROM course_scan_state
               WHERE account_scope=? AND platform=? AND scan_id=?""",
            (1 if tests_requested else 0, 1 if tests_requested else 0,
             scope, PLATFORM, scan_id)).fetchone()
        con.execute(
            "UPDATE scan_runs SET course_done=?,course_failed=? WHERE scan_id=?",
            (int(row["done"] or 0), int(row["failed"] or 0), scan_id))
    return {"homework_keys": keys_hw, "test_keys": keys_tests,
            "homework_complete": hw_complete, "tests_complete": test_complete}


def latest_state(scope: str | None = None) -> dict:
    init_db()
    scope = scope or account_scope()
    with connection() as con:
        scan = con.execute(
            """SELECT * FROM scan_runs WHERE account_scope=? AND platform=?
               ORDER BY started_at DESC LIMIT 1""", (scope, PLATFORM)).fetchone()
        states = con.execute(
            """SELECT * FROM course_scan_state WHERE account_scope=? AND platform=?
               ORDER BY course""", (scope, PLATFORM)).fetchall()
    return {"scope": scope, "scan": dict(scan) if scan else None,
            "courses": [dict(x) for x in states]}


def coverage(scope: str | None = None, *, expected_lids: Iterable[str] | None = None,
             tests_requested: bool = True, current_index_version: str = "",
             tests_blocked_lids: Iterable[str] | None = None,
             parse_version: str = PARSE_VERSION,
             max_age_sec: int | None = None) -> dict:
    init_db()
    scope = scope or account_scope()
    # None means "use the last scan's scope".  An explicit empty list means the
    # caller knows the scope is empty and must never fall back to stale data.
    explicit_expected = expected_lids is not None
    expected = set(str(x) for x in (expected_lids or []))
    with connection() as con:
        scan = con.execute(
            """SELECT * FROM scan_runs WHERE account_scope=? AND platform=?
               ORDER BY started_at DESC LIMIT 1""", (scope, PLATFORM)).fetchone()
        states = con.execute(
            """SELECT * FROM course_scan_state WHERE account_scope=? AND platform=?""",
            (scope, PLATFORM)).fetchall()
    by_lid = {str(x["lid"]): x for x in states}
    if not explicit_expected:
        expected = set(_json(scan["expected_json"], [])) if scan else set(by_lid)
    current_test_blocked = {str(x) for x in (tests_blocked_lids or [])}
    test_ok = {"ok", "empty", "no_entry"}
    missing, failed, scope_mismatch, tests_missing = [], [], [], []
    checked = []
    for lid in sorted(expected):
        state = by_lid.get(lid)
        if not state:
            missing.append(lid)
            continue
        if parse_version and state["parse_version"] != parse_version:
            scope_mismatch.append(lid)
        if current_index_version and state["index_version"] != current_index_version:
            scope_mismatch.append(lid)
        hw_status = state["homework_status"]
        if hw_status in ("error", "invalid", "incomplete", "auth"):
            failed.append(lid)
        elif hw_status not in ("ok", "empty"):
            missing.append(lid)
        tests_required = bool(tests_requested and lid not in current_test_blocked)
        if tests_required and state["tests_status"] not in test_ok:
            tests_missing.append(lid)
        if hw_status in ("ok", "empty") and state["homework_checked_at"]:
            checked.append(float(state["homework_checked_at"]))
        if tests_required and state["tests_status"] in test_ok and state["tests_checked_at"]:
            checked.append(float(state["tests_checked_at"]))
    missing = sorted(set(missing))
    failed = sorted(set(failed))
    scope_mismatch = sorted(set(scope_mismatch))
    tests_missing = sorted(set(tests_missing))
    # Publication freshness is bounded by the *oldest* course in the scope.
    # Taking max(checked_at) would let one single-course refresh hide stale
    # snapshots for every other course.
    updated = min(checked) if checked else None
    newest = max(checked) if checked else None
    age = (time.time() - updated) if updated else None
    reasons: list[str] = []
    if not expected:
        reasons.append("课程范围为空，无法确认完整覆盖")
    if missing:
        reasons.append(f"{len(missing)} 门课程缺少本次范围的完整列表")
    if failed:
        reasons.append(f"{len(failed)} 门课程本次列表抓取失败或未确认完整")
    if scope_mismatch:
        reasons.append(f"{len(scope_mismatch)} 门课程的索引/解析版本已变化")
    if tests_requested and tests_missing:
        reasons.append(f"{len(tests_missing)} 门课程缺少测试覆盖")
    if max_age_sec is not None and (age is None or age > max_age_sec):
        reasons.append(f"缓存已超过 {max_age_sec} 秒新鲜度要求")
    if scan and scan["status"] == "running":
        reasons.append("最近一次扫描仍在进行，完成后才能正式发布")
    if scan and scan["status"] not in ("running", "complete", "partial"):
        reasons.append(f"最近一次扫描状态为 {scan['status']}，不能复用更早的成功结果")
    if scan and scan["error"]:
        reasons.append(f"最近一次扫描失败：{str(scan['error'])[:160]}")
    ident = identity_status(scope)
    if not ident.get("verified"):
        reasons.append(ident.get("reason") or "平台账号身份未确认，禁止正式发布")
    return {
        "ok": True, "scope": scope, "updated_at": _iso(updated),
        "updated_epoch": updated, "newest_at": _iso(newest),
        "age_sec": round(age, 1) if age is not None else None,
        "publishable": not reasons, "publish_block_reasons": reasons,
        "missing_courses": missing, "failed_courses": failed,
        "scope_mismatch_courses": scope_mismatch,
        "tests_missing_courses": tests_missing,
        "expected_courses": len(expected), "known_courses": len(by_lid),
        "tests_requested": bool(tests_requested),
        "tests_blocked_courses": sorted(current_test_blocked),
        "identity": ident,
        "scan": scan_progress(scan["scan_id"], scope) if scan else None,
        "last_scan_status": scan["status"] if scan else None,
    }


def load_items(scope: str | None = None, *, include_inactive: bool = False,
               exclude_lids: Iterable[str] | None = None,
               exclude_test_lids: Iterable[str] | None = None) -> list[dict]:
    init_db()
    scope = scope or account_scope()
    excluded = {str(x) for x in (exclude_lids or [])}
    excluded_tests = {str(x) for x in (exclude_test_lids or [])}
    where = "" if include_inactive else " AND a.active=1"
    with connection() as con:
        generation = _scope_identity_generation(scope, con)
        rows = con.execute(
            f"""SELECT a.*, c.status AS content_status, c.source_checked_at,
                       c.ttl_sec AS content_ttl_sec, c.source_hash, c.source_version,
                       c.identity_generation AS content_identity_generation,
                       c.revision AS content_revision, c.blocks_json,
                       c.text AS content_text, c.asset_ids_json,
                       c.error AS content_error
                FROM assignments a LEFT JOIN assignment_contents c
                  ON c.item_key=a.item_key
                WHERE a.account_scope=? AND a.platform=?{where}
                ORDER BY a.deadline, a.course, a.kind""",
            (scope, PLATFORM)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            extra = _json(item.pop("extra_json", None), {})
            for key, value in extra.items():
                item.setdefault(key, value)
            item["submitted"] = (None if item.get("submitted") is None
                                 else bool(item.get("submitted")))
            content_generation = int(item.get("content_identity_generation") or 0)
            identity_current = (not item.get("content_status")
                                or content_generation == generation)
            if not identity_current:
                # Never expose another account's body through the normal cache.
                # The row and manual views stay in SQLite for offline export.
                item["content_identity_stale"] = True
                item["content_status"] = "identity_changed"
                item["content_error"] = _IDENTITY_STALE_REASON
                item["source_version"] = None
                item["content_revision"] = None
                item["source_checked_at"] = None
                item["content_text"] = ""
                item["blocks_json"] = "[]"
                item["asset_ids_json"] = "[]"
            item["identity_generation"] = generation
            version = item.get("source_version")
            if version:
                view = con.execute(
                    """SELECT * FROM content_views WHERE item_key=? AND source_version=?
                       AND view_key='default'""", (item["item_key"], version)).fetchone()
                if view and view["review_status"] == "needs_review":
                    item["stale_view"] = {
                        "source_version": view["source_version"],
                        "review_status": view["review_status"]}
                elif not view:
                    stale = con.execute(
                        """SELECT source_version, review_status FROM content_views
                           WHERE item_key=? ORDER BY updated_at DESC LIMIT 1""",
                        (item["item_key"],)).fetchone()
                    if stale:
                        item["stale_view"] = {
                            "source_version": stale["source_version"],
                            "review_status": stale["review_status"]}
                else:
                    item["view"] = {
                        "source_version": view["source_version"],
                        "regions": _json(view["regions_json"], []),
                        "folded_block_ids": _json(view["folded_json"], []),
                        "summary": view["summary"],
                        "review_status": view["review_status"],
                        "updated_at": _iso(view["updated_at"])}
            elif not identity_current:
                stale = con.execute(
                    """SELECT source_version, review_status FROM content_views
                       WHERE item_key=? ORDER BY updated_at DESC LIMIT 1""",
                    (item["item_key"],)).fetchone()
                if stale:
                    item["stale_view"] = {
                        "source_version": stale["source_version"],
                        "review_status": stale["review_status"]}
            item["content_blocks"] = _json(item.pop("blocks_json", None), [])
            item["asset_ids"] = _json(item.pop("asset_ids_json", None), [])
            checked = item.get("source_checked_at")
            ttl = int(item.get("content_ttl_sec") or cache_settings()["content_ttl_sec"])
            item["content_checked_at"] = _iso(checked)
            item["content_stale"] = bool(not identity_current
                                         or (checked and time.time() - checked > ttl))
            item["content_has_blocks"] = bool(item["content_blocks"])
            item["content_ready"] = (item.get("content_status") in ("ready", "stale_error")
                                     and bool(item["content_blocks"]))
            item["kind"] = item.get("kind") or "homework"
            if item["kind"] == "homework" and item.get("platform_id"):
                item["hwtid"] = item["platform_id"]
            elif item["kind"] == "test" and item.get("platform_id"):
                item["test_id"] = item["platform_id"]
            if str(item.get("lid")) in excluded:
                continue
            if item["kind"] == "test" and str(item.get("lid")) in excluded_tests:
                continue
            out.append(item)
    return out


def get_item(item_key: str, scope: str | None = None) -> dict | None:
    for item in load_items(scope, include_inactive=True):
        if item["item_key"] == item_key:
            return item
    return None


def content_due(scope: str | None = None, *, days: int = 14, limit: int | None = None,
                force: bool = False, item_keys: Iterable[str] | None = None,
                exclude_lids: Iterable[str] | None = None,
                exclude_test_lids: Iterable[str] | None = None,
                now: datetime | None = None) -> list[dict]:
    scope = scope or account_scope()
    now = now or datetime.now()
    wanted = {str(x) for x in (item_keys or []) if x}
    out = []
    for item in load_items(scope, exclude_lids=exclude_lids,
                           exclude_test_lids=exclude_test_lids):
        if item.get("kind") != "homework" or not item.get("hwtid"):
            continue
        if wanted and item.get("item_key") not in wanted:
            continue
        deadline = str(item.get("deadline") or "")
        try:
            dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                dt = datetime.strptime(deadline, "%Y-%m-%d").replace(hour=23, minute=59)
            except ValueError:
                continue
        if dt < now or (dt - now).days > days:
            continue
        if force or not item.get("content_ready") or item.get("content_stale") or item.get("content_error"):
            out.append(item)
    out.sort(key=lambda x: (x.get("deadline") or "", x.get("course") or ""))
    return out if limit is None or int(limit) <= 0 else out[:int(limit)]


def save_content(item_key: str, result: dict, *, ttl_sec: int | None = None,
                 scope: str | None = None,
                 expected_identity_generation: int | None = None) -> dict:
    init_db()
    scope = scope or account_scope()
    now = time.time()
    ttl = int(ttl_sec or cache_settings()["content_ttl_sec"])
    with connection(write=True) as con:
        generation = _scope_identity_generation(scope, con)
        owner = con.execute(
            "SELECT account_scope FROM assignments WHERE item_key=?",
            (item_key,)).fetchone()
        if owner and owner["account_scope"] != scope:
            raise VersionConflict("条目不属于当前账号范围，拒写正文")
        if (expected_identity_generation is not None
                and int(expected_identity_generation) != generation):
            raise VersionConflict("账号身份已变更，旧扫描结果不能写回正文")
        old = con.execute(
            """SELECT source_version,revision,identity_generation
               FROM assignment_contents WHERE item_key=?""", (item_key,)).fetchone()
        same_identity = bool(
            old and int(old["identity_generation"] or 0) == generation)
        old_version = old["source_version"] if same_identity else None
        new_version = result.get("source_version")
        # Identity changes must invalidate even an open editor whose source
        # bytes happen to be identical under the new account.
        if new_version and generation:
            new_version = version_hash({"source": new_version, "identity_generation": generation})
        revision = int(old["revision"] if old else 0)
        if new_version and (not same_identity or new_version != old_version):
            revision += 1
            con.execute(
                """UPDATE content_views SET review_status='needs_review'
                   WHERE item_key=? AND source_version<>?""",
                (item_key, new_version))
        con.execute(
            """INSERT INTO assignment_contents(item_key,status,source_url,
               source_checked_at,ttl_sec,source_hash,source_version,
               identity_generation,revision,raw_html,blocks_json,text,
               asset_ids_json,error,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_key) DO UPDATE SET
                 status=excluded.status,source_url=excluded.source_url,
                 source_checked_at=excluded.source_checked_at,ttl_sec=excluded.ttl_sec,
                 source_hash=excluded.source_hash,source_version=excluded.source_version,
                 identity_generation=excluded.identity_generation,
                 revision=excluded.revision,raw_html=excluded.raw_html,
                 blocks_json=excluded.blocks_json,text=excluded.text,
                 asset_ids_json=excluded.asset_ids_json,error=excluded.error,
                 updated_at=excluded.updated_at""",
            (item_key, result.get("status", "ready"), result.get("source_url"),
             now, ttl, result.get("source_hash"), new_version, generation, revision,
             result.get("raw_html"), json.dumps(result.get("blocks") or [], ensure_ascii=False),
             result.get("text", ""), json.dumps(result.get("asset_ids") or [], ensure_ascii=False),
             result.get("error"), now),
        )
        for asset in result.get("assets") or []:
            con.execute(
                """INSERT INTO assets(asset_id,sha256,path,mime,width,height,bytes,
                   source_url,checked_at,original_path,display_path,
                   orientation_corrected,crop_safe)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset_id) DO UPDATE SET
                     path=excluded.path,display_path=excluded.display_path,
                     original_path=excluded.original_path,
                     orientation_corrected=excluded.orientation_corrected,
                     crop_safe=excluded.crop_safe,checked_at=excluded.checked_at""",
                (asset["asset_id"], asset["sha256"], asset["path"], asset["mime"],
                 asset["width"], asset["height"], asset["bytes"],
                 asset.get("source_url"), now,
                 asset.get("original_path") or asset["path"],
                 asset.get("display_path") or asset["path"],
                 1 if asset.get("orientation_corrected") else 0,
                 1 if asset.get("crop_safe", True) else 0))
    return {"item_key": item_key, "source_version": new_version,
            "identity_generation": generation, "revision": revision}


def record_content_error(item_key: str, error: str, *, source_url: str | None = None,
                         scope: str | None = None,
                         expected_identity_generation: int | None = None) -> bool:
    init_db()
    now = time.time()
    with connection(write=True) as con:
        owner = con.execute(
            "SELECT account_scope FROM assignments WHERE item_key=?",
            (item_key,)).fetchone()
        scope = scope or (owner["account_scope"] if owner else account_scope())
        generation = _scope_identity_generation(scope, con)
        if (expected_identity_generation is not None
                and int(expected_identity_generation) != generation):
            return False
        content = con.execute(
            """SELECT identity_generation FROM assignment_contents
               WHERE item_key=?""", (item_key,)).fetchone()
        if content and int(content["identity_generation"] or 0) != generation:
            return False
        if content:
            con.execute(
                """UPDATE assignment_contents SET status='stale_error',error=?,
                   updated_at=? WHERE item_key=?""", (error[:500], now, item_key))
        else:
            con.execute(
                """INSERT INTO assignment_contents(item_key,status,source_url,
                   ttl_sec,identity_generation,error,updated_at)
                   VALUES(?,'error',?,?,?,?,?)""",
                (item_key, source_url, cache_settings()["content_ttl_sec"],
                 generation, error[:500], now))
    return True


class VersionConflict(RuntimeError):
    pass


def _validate_regions(regions: Any) -> list[dict]:
    if not isinstance(regions, list) or len(regions) > 20:
        raise ValueError("裁剪区域必须是数组（最多 20 个）")
    clean = []
    for region in regions:
        try:
            asset_id = str(region["asset_id"])
            box = [float(x) for x in region["box"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("裁剪区域格式错误") from exc
        if len(box) != 4:
            raise ValueError("裁剪框必须是归一化 xyxy 四个值")
        x1, y1, x2, y2 = box
        if not all(math.isfinite(x) and 0.0 <= x <= 1.0 for x in box):
            raise ValueError("裁剪框超出 0–1 或包含非数值")
        if x1 >= x2 or y1 >= y2:
            raise ValueError("裁剪框超出 0–1 或不是有效 xyxy")
        clean.append({"asset_id": asset_id, "box": [x1, y1, x2, y2],
                      "origin": str(region.get("origin") or "manual")})
    return clean


def save_view(item_key: str, *, expected_version: str, regions: list[dict],
              folded_block_ids: list[str] | None = None, summary: str = "",
              review_status: str = "ok") -> dict:
    init_db()
    regions = _validate_regions(regions)
    folded = [str(x) for x in (folded_block_ids or [])]
    now = time.time()
    with connection(write=True) as con:
        content = con.execute(
            """SELECT c.source_version,c.asset_ids_json,c.identity_generation,
                      a.account_scope
               FROM assignment_contents c
               LEFT JOIN assignments a ON a.item_key=c.item_key
               WHERE c.item_key=?""", (item_key,)).fetchone()
        current = content["source_version"] if content else None
        if not current:
            raise VersionConflict("正文尚未就绪")
        scope = (content["account_scope"] if content and content["account_scope"]
                 else account_scope())
        generation = _scope_identity_generation(scope, con)
        if int(content["identity_generation"] or 0) != generation:
            raise VersionConflict("正文属于旧账号缓存，需重新抓取并复核后再保存裁剪")
        if expected_version != current:
            raise VersionConflict("正文版本已更新，请重新加载后再保存裁剪")
        allowed_assets = set(_json(content["asset_ids_json"], []))
        unknown_assets = sorted({r["asset_id"] for r in regions} - allowed_assets)
        if unknown_assets:
            raise ValueError("裁剪区域引用了不属于当前正文版本的图片")
        if regions:
            region_asset_ids = sorted({r["asset_id"] for r in regions})
            asset_rows = con.execute(
                f"SELECT asset_id,crop_safe FROM assets WHERE asset_id IN "
                f"({','.join('?' for _ in region_asset_ids)})",
                tuple(region_asset_ids)).fetchall()
            unsafe = [r["asset_id"] for r in asset_rows if not int(r["crop_safe"] or 0)]
            if unsafe:
                raise ValueError("该图片未经 Pillow 解码/方向校验，已禁用裁剪；"
                                 "请安装 Pillow 后刷新正文")
        con.execute(
            """INSERT INTO content_views(item_key,source_version,view_key,
               regions_json,folded_json,summary,review_status,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(item_key,source_version,view_key) DO UPDATE SET
                 regions_json=excluded.regions_json,folded_json=excluded.folded_json,
                 summary=excluded.summary,review_status=excluded.review_status,
                 updated_at=excluded.updated_at""",
            (item_key, current, "default", json.dumps(regions, ensure_ascii=False),
             json.dumps(folded, ensure_ascii=False), summary[:2000],
             review_status, now),
        )
    return {"item_key": item_key, "source_version": current,
            "regions": regions, "folded_block_ids": folded,
            "summary": summary, "review_status": review_status,
            "updated_at": _iso(now)}


def export_views(scope: str | None = None) -> dict:
    init_db()
    scope = scope or account_scope()
    with connection() as con:
        rows = con.execute(
            """SELECT v.*, a.course, a.title, a.kind
               FROM content_views v JOIN assignments a ON a.item_key=v.item_key
               WHERE a.account_scope=?
               ORDER BY a.course,a.title,v.updated_at""", (scope,)).fetchall()
    return {"ok": True, "exported_at": _iso(time.time()), "scope": scope,
            "views": [{**dict(r), "regions": _json(r["regions_json"], []),
                       "folded_block_ids": _json(r["folded_json"], [])}
                      for r in rows]}


def register_task(task_id: str, signature: str, op: str, params: dict) -> tuple[str, bool]:
    """Register a task, returning an already active task with the same range."""
    init_db()
    now = time.time()
    recover_orphan_tasks()
    from platform_lock import process_info
    _, owner_started = process_info(os.getpid())
    with connection(write=True) as con:
        existing = con.execute(
            """SELECT task_id,owner_pid,created_at FROM task_registry WHERE signature=?
               AND status IN ('queued','running') ORDER BY created_at LIMIT 1""",
            (signature,)).fetchone()
        if existing:
            status, _ = process_info(int(existing["owner_pid"] or 0))
            if int(existing["owner_pid"] or 0) == os.getpid() or status == "alive":
                return existing["task_id"], True
            # The owning process is gone; do not let an orphan block retries.
            con.execute(
                """UPDATE task_registry SET status='error',finished_at=?,
                   result_summary='owner process exited' WHERE task_id=?""",
                (now, existing["task_id"]))
        con.execute(
            """INSERT INTO task_registry(task_id,signature,op,params_json,status,
               created_at,owner_pid,owner_started) VALUES(?,?,?,?,'queued',?,?,?)""",
            (task_id, signature, op, json.dumps(params, ensure_ascii=False, sort_keys=True),
             now, os.getpid(), owner_started))
    return task_id, False


def recover_orphan_tasks() -> int:
    """Mark queued/running tasks whose owning process is gone as errors."""
    init_db()
    from platform_lock import process_info
    now = time.time()
    recovered = 0
    with connection(write=True) as con:
        rows = con.execute(
            """SELECT task_id,owner_pid,owner_started FROM task_registry
               WHERE status IN ('queued','running')""").fetchall()
        for row in rows:
            pid = int(row["owner_pid"] or 0)
            if pid == os.getpid():
                continue
            status, started = process_info(pid)
            same_owner = (status == "alive" and row["owner_started"]
                          and started and int(row["owner_started"]) == int(started))
            # Unknown start stamps are treated conservatively while the PID is
            # alive; only a proven dead/reused PID is reclaimed.
            if same_owner:
                continue
            reused = (status == "alive" and row["owner_started"] and started
                      and int(row["owner_started"]) != int(started))
            if status != "dead" and not reused:
                continue
            con.execute(
                """UPDATE task_registry SET status='error',finished_at=?,
                   result_summary='owner process exited before completion'
                   WHERE task_id=?""", (now, row["task_id"]))
            recovered += 1
    return recovered


def task_record(task_id: str) -> dict | None:
    init_db()
    with connection() as con:
        row = con.execute("SELECT * FROM task_registry WHERE task_id=?",
                          (task_id,)).fetchone()
    return dict(row) if row else None


def update_task(task_id: str, status: str, *, result_summary: Any = None,
                scan_id: str | None = None) -> None:
    now = time.time()
    sets = ["status=?"]
    vals: list[Any] = [status]
    if status == "running":
        sets.append("started_at=?")
        vals.append(now)
    if status in ("done", "partial", "error", "cancelled"):
        sets.append("finished_at=?")
        vals.append(now)
    if result_summary is not None:
        sets.append("result_summary=?")
        vals.append(json.dumps(result_summary, ensure_ascii=False, default=str)[:4000])
    if scan_id:
        sets.append("scan_id=?")
        vals.append(str(scan_id))
    vals.append(task_id)
    with connection(write=True) as con:
        con.execute(f"UPDATE task_registry SET {','.join(sets)} WHERE task_id=?", vals)


def active_scan_id(scope: str | None = None) -> str | None:
    init_db()
    scope = scope or account_scope()
    with connection() as con:
        row = con.execute(
            """SELECT scan_id FROM scan_runs WHERE account_scope=? AND platform=?
               AND status='running' ORDER BY started_at DESC LIMIT 1""",
            (scope, PLATFORM)).fetchone()
    return row["scan_id"] if row else None


def asset_record(asset_id: str) -> dict | None:
    init_db()
    with connection() as con:
        row = con.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    return dict(row) if row else None
