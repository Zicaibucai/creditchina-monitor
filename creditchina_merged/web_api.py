"""本机真实采集 API。

服务复用 ``spider_main.py`` 使用的 BrowserClient 和
CreditChinaCrawler，并把任务、实际采集结果与只增不减的历史
保存在本机 SQLite 中。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from fastapi import FastAPI, HTTPException, Query
from fastapi.background import BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .browser_client import BrowserClient
from .config import PROJECT_ROOT, ApiConfig, HttpConfig, proxies_from_values
from .crawler import CreditChinaCrawler, EnterpriseRecord
from .exporter import XlsxExporter
from .http_client import AccessIntercepted, ProxyUnavailable, RequestFailed
from .proxy_provider import KuaidailiPrivateProxyProvider, kuaidaili_enabled
from .sh_zjw_spider import ShZjwSpider, default_company_code
from .storage import FileStorage, safe_filename


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_STATE_PATH = DEFAULT_OUTPUT_DIR / "creditchina_api.sqlite3"
DEFAULT_COMPANY_FILE = PROJECT_ROOT / "monitor_companies.txt"


def _project_path_from_env(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _utc_timestamp() -> float:
    return time.time()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _penalty_identity(item: Mapping[str, Any]) -> str:
    for key in ("决定书文号", "行政处罚决定书文号"):
        candidate = str(item.get(key, "")).strip()
        if candidate:
            return candidate
    raw = item.get("_原始字段")
    if isinstance(raw, Mapping):
        for key in ("recid", "uuid", "flowno"):
            candidate = str(raw.get(key, "")).strip()
            if candidate:
                return candidate
    return _fingerprint(item)


def _record_key(section: str, item: Mapping[str, Any]) -> str:
    keys = {
        "基本信息": ("统一社会信用代码", "工商注册号"),
        "行政许可": ("行政许可决定书文号", "许可机关", "许可决定日期"),
        "行政处罚": ("决定书文号", "处罚名称", "处罚决定日期"),
        "守信红名单": ("序号", "评价年度", "文件名"),
        "重点关注名单": ("注册号", "最新更新日期", "列入决定机关名称"),
        "黑名单": ("案号", "执行依据文号", "立案时间"),
    }.get(section, ())
    parts = [str(item.get(key, "")).strip() for key in keys]
    return " | ".join(part for part in parts if part)[:512]


def _record_payload(record: EnterpriseRecord) -> Dict[str, Any]:
    return {
        "name": record.name,
        "encry_str": record.encry_str,
        "basic": record.basic,
        "permissions": record.permissions,
        "penalties": record.penalties,
        "red_list": record.red_list,
        "watch_list": record.watch_list,
        "black_list": record.black_list,
        "errors": record.errors,
    }


def _record_from_payload(payload: Mapping[str, Any]) -> EnterpriseRecord:
    return EnterpriseRecord(
        name=str(payload.get("name", "")),
        encry_str=str(payload.get("encry_str", "")),
        basic=dict(payload.get("basic") or {}),
        permissions=list(payload.get("permissions") or []),
        penalties=list(payload.get("penalties") or []),
        red_list=list(payload.get("red_list") or []),
        watch_list=list(payload.get("watch_list") or []),
        black_list=list(payload.get("black_list") or []),
        errors=dict(payload.get("errors") or {}),
    )


class TaskStore:
    """线程安全的 SQLite 任务与历史仓。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  company_names TEXT NOT NULL,
                  status TEXT NOT NULL,
                  progress INTEGER NOT NULL DEFAULT 0,
                  completed INTEGER NOT NULL DEFAULT 0,
                  total INTEGER NOT NULL,
                  speed TEXT NOT NULL DEFAULT '--',
                  current_company TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  checkpoint_company TEXT NOT NULL DEFAULT '',
                  checkpoint_payload TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

                CREATE TABLE IF NOT EXISTS latest_records (
                  company_name TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  collected_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_records (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  company_name TEXT NOT NULL,
                  section_name TEXT NOT NULL,
                  record_key TEXT NOT NULL DEFAULT '',
                  fingerprint TEXT NOT NULL,
                  raw_json TEXT NOT NULL,
                  first_seen_at REAL NOT NULL,
                  last_seen_at REAL NOT NULL,
                  seen_count INTEGER NOT NULL DEFAULT 1,
                  UNIQUE(company_name, section_name, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_history_section_company
                  ON history_records(section_name, company_name);

                CREATE TABLE IF NOT EXISTS announcements (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  company_name TEXT NOT NULL,
                  section_name TEXT NOT NULL,
                  change_count INTEGER NOT NULL,
                  summary TEXT NOT NULL,
                  change_type TEXT NOT NULL DEFAULT 'added',
                  record_key TEXT NOT NULL DEFAULT '',
                  before_json TEXT NOT NULL DEFAULT '',
                  after_json TEXT NOT NULL DEFAULT '',
                  before_evidence TEXT NOT NULL DEFAULT '',
                  after_evidence TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_announcements_created
                  ON announcements(created_at DESC);

                CREATE TABLE IF NOT EXISTS monitor_runs (
                  run_date TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS credit_score_jobs (
                  id TEXT PRIMARY KEY,
                  company_names TEXT NOT NULL,
                  status TEXT NOT NULL,
                  progress INTEGER NOT NULL DEFAULT 0,
                  completed INTEGER NOT NULL DEFAULT 0,
                  total INTEGER NOT NULL,
                  current_company TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_credit_score_jobs_created
                  ON credit_score_jobs(created_at DESC);

                CREATE TABLE IF NOT EXISTS credit_scores (
                  company_name TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  score_total REAL,
                  report_date TEXT NOT NULL DEFAULT '',
                  collected_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_credit_scores_collected
                  ON credit_scores(collected_at DESC);

                CREATE TABLE IF NOT EXISTS evidence_captures (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  company_name TEXT NOT NULL,
                  penalty_count INTEGER NOT NULL DEFAULT 0,
                  source_url TEXT NOT NULL DEFAULT '',
                  overview_path TEXT NOT NULL DEFAULT '',
                  panel_path TEXT NOT NULL DEFAULT '',
                  html_path TEXT NOT NULL DEFAULT '',
                  metadata_path TEXT NOT NULL DEFAULT '',
                  overview_sha256 TEXT NOT NULL DEFAULT '',
                  captured_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_captures_company
                  ON evidence_captures(company_name, captured_at DESC, id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_captures_metadata
                  ON evidence_captures(metadata_path) WHERE metadata_path <> '';

                CREATE TABLE IF NOT EXISTS penalty_evidence (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  capture_id INTEGER NOT NULL DEFAULT 0,
                  company_name TEXT NOT NULL,
                  identity_key TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  screenshot_path TEXT NOT NULL DEFAULT '',
                  overview_path TEXT NOT NULL DEFAULT '',
                  metadata_path TEXT NOT NULL DEFAULT '',
                  screenshot_sha256 TEXT NOT NULL DEFAULT '',
                  captured_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_penalty_evidence_lookup
                  ON penalty_evidence(company_name, identity_key, captured_at DESC);
                """
            )
            announcement_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(announcements)").fetchall()
            }
            task_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            for column in ("checkpoint_company", "checkpoint_payload"):
                if column not in task_columns:
                    connection.execute(
                        "ALTER TABLE tasks ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % column
                    )
            penalty_evidence_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(penalty_evidence)").fetchall()
            }
            if "capture_id" not in penalty_evidence_columns:
                connection.execute(
                    "ALTER TABLE penalty_evidence ADD COLUMN capture_id INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_penalty_evidence_capture ON penalty_evidence(capture_id, id)"
            )
            for column, definition in (
                ("change_type", "TEXT NOT NULL DEFAULT 'added'"),
                ("record_key", "TEXT NOT NULL DEFAULT ''"),
                ("before_json", "TEXT NOT NULL DEFAULT ''"),
                ("after_json", "TEXT NOT NULL DEFAULT ''"),
                ("before_evidence", "TEXT NOT NULL DEFAULT ''"),
                ("after_evidence", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in announcement_columns:
                    connection.execute("ALTER TABLE announcements ADD COLUMN %s %s" % (column, definition))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def serialize_task(row: Mapping[str, Any]) -> Dict[str, Any]:
        status = str(row["status"])
        created_at = datetime.fromtimestamp(float(row["created_at"]))
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "scope": "企业清单 · %d 家" % int(row["total"]),
            "companyNames": json.loads(str(row["company_names"])),
            "progress": int(row["progress"]),
            "completed": int(row["completed"]),
            "total": int(row["total"]),
            "status": status,
            "speed": str(row["speed"]) if status == "running" else "--",
            "currentCompany": str(row["current_company"]),
            "error": str(row["error"]),
            "created": created_at.strftime("%H:%M"),
        }

    def create_task(self, name: str, companies: Sequence[str]) -> Dict[str, Any]:
        task_id = str(uuid.uuid4())
        now = _utc_timestamp()
        display_name = name.strip()
        if not display_name or display_name == "企业信用信息采集":
            display_name = companies[0] if len(companies) == 1 else "%s等 %d 家企业" % (companies[0], len(companies))
        connection = self.connect()
        try:
            connection.execute(
                """
                INSERT INTO tasks
                  (id, name, company_names, status, progress, completed, total,
                   speed, current_company, error, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', 0, 0, ?, '--', '', '', ?, ?)
                """,
                (task_id, display_name, json.dumps(list(companies), ensure_ascii=False), len(companies), now, now),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            assert row is not None
            return self.serialize_task(row)
        finally:
            connection.close()

    def list_tasks(self) -> List[Dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200").fetchall()
            return [self.serialize_task(row) for row in rows]
        finally:
            connection.close()

    def raw_task(self, task_id: str) -> Optional[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            connection.close()

    def update_task(self, task_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "progress",
            "completed",
            "speed",
            "current_company",
            "error",
            "checkpoint_company",
            "checkpoint_payload",
        }
        selected = {key: value for key, value in values.items() if key in allowed}
        if not selected:
            return
        selected["updated_at"] = _utc_timestamp()
        columns = ", ".join("%s = ?" % key for key in selected)
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE tasks SET %s WHERE id = ?" % columns,
                tuple(selected.values()) + (task_id,),
            )
            connection.commit()
        finally:
            connection.close()

    def recover_tasks(self) -> List[str]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE status IN ('queued', 'running', 'pause_requested') ORDER BY created_at"
            ).fetchall()
            connection.execute(
                "UPDATE tasks SET status = 'queued', speed = '--', current_company = '' WHERE status IN ('running', 'pause_requested')"
            )
            connection.commit()
            return [str(row["id"]) for row in rows]
        finally:
            connection.close()

    def set_action(self, task_id: str, action: str) -> Dict[str, Any]:
        row = self.raw_task(task_id)
        if row is None:
            raise KeyError(task_id)
        status = str(row["status"])
        if action == "pause" and status in ("queued", "running"):
            self.update_task(task_id, status="pause_requested" if status == "running" else "paused", speed="--")
        elif action == "resume" and status in ("paused", "pause_requested", "failed", "intercepted"):
            self.update_task(task_id, status="queued", error="")
        else:
            raise ValueError("当前任务状态不支持该操作")
        row = self.raw_task(task_id)
        assert row is not None
        return self.serialize_task(row)

    def delete_task(self, task_id: str) -> None:
        connection = self.connect()
        try:
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            connection.commit()
        finally:
            connection.close()

    def cancel_all_work(self) -> Dict[str, List[str]]:
        """把所有尚未结束的行政采集和信用分任务统一标记为取消。"""

        connection = self.connect()
        try:
            task_rows = connection.execute(
                """
                SELECT id FROM tasks
                WHERE status IN ('queued', 'running', 'paused', 'pause_requested')
                """
            ).fetchall()
            credit_rows = connection.execute(
                """
                SELECT id FROM credit_score_jobs
                WHERE status IN ('queued', 'running')
                """
            ).fetchall()
            now = _utc_timestamp()
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', speed = '--', current_company = '',
                    error = '用户已停止任务', updated_at = ?
                WHERE status IN ('queued', 'running', 'paused', 'pause_requested')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE credit_score_jobs
                SET status = 'cancelled', current_company = '',
                    error = '用户已停止任务', updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            connection.commit()
            return {
                "tasks": [str(row["id"]) for row in task_rows],
                "creditScoreJobs": [str(row["id"]) for row in credit_rows],
            }
        finally:
            connection.close()

    def save_record(
        self,
        record: EnterpriseRecord,
        evidence: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, int]:
        payload = _record_payload(record)
        now = _utc_timestamp()
        sections: Iterable[tuple[str, Sequence[Mapping[str, Any]]]] = (
            ("基本信息", [record.basic] if record.basic else []),
            ("行政许可", record.permissions),
            ("行政处罚", record.penalties),
        )
        connection = self.connect()
        try:
            previous_row = connection.execute(
                "SELECT payload_json FROM latest_records WHERE company_name = ?",
                (record.name,),
            ).fetchone()
            had_previous = previous_row is not None
            previous_record = (
                _record_from_payload(json.loads(str(previous_row["payload_json"])))
                if previous_row is not None
                else None
            )
            previous_penalties = previous_record.penalties if previous_record else []
            effective_penalties = (
                previous_penalties
                if previous_record is not None and "行政处罚" in record.errors
                else record.penalties
            )
            payload["penalties"] = effective_penalties
            previous_by_identity = {_penalty_identity(item): item for item in previous_penalties}
            current_by_identity = {_penalty_identity(item): item for item in effective_penalties}
            previous_evidence: Dict[str, str] = {}
            for identity in previous_by_identity:
                row = connection.execute(
                    "SELECT screenshot_path FROM penalty_evidence WHERE company_name = ? AND identity_key = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
                    (record.name, identity),
                ).fetchone()
                previous_evidence[identity] = str(row["screenshot_path"]) if row else ""

            evidence_items = {
                str(item.get("identity", "")): item
                for item in ((evidence or {}).get("items") or [])
                if item.get("identity")
            }
            overview_evidence = str((evidence or {}).get("overview_path", ""))
            panel_evidence = str((evidence or {}).get("penalty_panel_path", ""))
            html_evidence = str((evidence or {}).get("html_path", ""))
            metadata_evidence = str((evidence or {}).get("metadata_path", ""))
            capture_id = 0
            if any((overview_evidence, panel_evidence, html_evidence, metadata_evidence)):
                existing_capture = (
                    connection.execute(
                        "SELECT id FROM evidence_captures WHERE metadata_path = ?",
                        (metadata_evidence,),
                    ).fetchone()
                    if metadata_evidence
                    else None
                )
                if existing_capture is not None:
                    capture_id = int(existing_capture["id"])
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO evidence_captures
                          (company_name, penalty_count, source_url, overview_path,
                           panel_path, html_path, metadata_path, overview_sha256,
                           captured_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.name,
                            int((evidence or {}).get("penalty_count_page", len(record.penalties))),
                            str((evidence or {}).get("source_url", "")),
                            overview_evidence,
                            panel_evidence,
                            html_evidence,
                            metadata_evidence,
                            str((evidence or {}).get("overview_sha256", "")),
                            now,
                        ),
                    )
                    capture_id = int(cursor.lastrowid)
            written_evidence_identities = set()
            # 先记录官网 DOM 中实际截到的每一条。即使 JSON 接口当次
            # 失败或条数不一致，截图仍然可在网页端调用并留作证据。
            for identity, captured in evidence_items.items():
                item = current_by_identity.get(identity, {})
                connection.execute(
                    """
                    INSERT INTO penalty_evidence
                      (capture_id, company_name, identity_key, fingerprint, screenshot_path,
                       overview_path, metadata_path, screenshot_sha256, captured_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        capture_id,
                        record.name,
                        identity,
                        _fingerprint(item) if item else hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                        str(captured.get("screenshot_path", "")),
                        overview_evidence,
                        str((evidence or {}).get("metadata_path", "")),
                        str(captured.get("sha256", "")),
                        now,
                    ),
                )
                written_evidence_identities.add(identity)
            # 如果接口有处罚条目但页面未产生逐条截图，仍保留该条
            # 的证据索引，便于后续变更公告关联整页图。
            for identity, item in current_by_identity.items():
                if identity in written_evidence_identities:
                    continue
                connection.execute(
                    """
                    INSERT INTO penalty_evidence
                      (capture_id, company_name, identity_key, fingerprint, screenshot_path,
                       overview_path, metadata_path, screenshot_sha256, captured_at)
                    VALUES (?, ?, ?, ?, '', ?, ?, '', ?)
                    """,
                    (
                        capture_id,
                        record.name,
                        identity,
                        _fingerprint(item),
                        overview_evidence,
                        metadata_evidence,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO latest_records(company_name, payload_json, collected_at)
                VALUES (?, ?, ?)
                ON CONFLICT(company_name) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  collected_at = excluded.collected_at
                """,
                (record.name, json.dumps(payload, ensure_ascii=False, default=str), now),
            )
            new_counts: Dict[str, int] = {}
            for section, items in sections:
                for item in items:
                    raw_json = _canonical_json(item)
                    cursor = connection.execute(
                        """
                        INSERT INTO history_records
                          (company_name, section_name, record_key, fingerprint, raw_json,
                           first_seen_at, last_seen_at, seen_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(company_name, section_name, fingerprint) DO UPDATE SET
                          record_key = excluded.record_key,
                          last_seen_at = excluded.last_seen_at,
                          seen_count = history_records.seen_count + 1
                        """,
                        (
                            record.name,
                            section,
                            _record_key(section, item),
                            _fingerprint(item),
                            raw_json,
                            now,
                            now,
                        ),
                    )
                    if cursor.rowcount == 1:
                        inserted = connection.execute(
                            "SELECT seen_count FROM history_records WHERE company_name = ? AND section_name = ? AND fingerprint = ?",
                            (record.name, section, _fingerprint(item)),
                        ).fetchone()
                        if inserted is not None and int(inserted["seen_count"]) == 1:
                            new_counts[section] = new_counts.get(section, 0) + 1
            if had_previous:
                for section, count in new_counts.items():
                    if section == "行政处罚":
                        continue
                    connection.execute(
                        "INSERT INTO announcements(company_name, section_name, change_count, summary, change_type, created_at) VALUES (?, ?, ?, ?, 'added', ?)",
                        (record.name, section, count, "%s新增或变更 %d 条" % (section, count), now),
                    )

            # 处罚接口失败时不能把空列表误判为“删除”；只有本次处罚栏目成功时
            # 才执行双向差异比较。
            if had_previous and "行政处罚" not in record.errors:
                added = sorted(set(current_by_identity) - set(previous_by_identity))
                deleted = sorted(set(previous_by_identity) - set(current_by_identity))
                modified = sorted(
                    identity
                    for identity in set(previous_by_identity) & set(current_by_identity)
                    if _fingerprint(previous_by_identity[identity]) != _fingerprint(current_by_identity[identity])
                )
                event_rows = []
                for change_type, identities in (("added", added), ("deleted", deleted), ("modified", modified)):
                    for identity in identities:
                        before = previous_by_identity.get(identity)
                        after = current_by_identity.get(identity)
                        document = str((after or before or {}).get("决定书文号") or (after or before or {}).get("行政处罚决定书文号") or identity)
                        if change_type == "added":
                            summary = "新增行政处罚：%s" % document
                        elif change_type == "deleted":
                            summary = "行政处罚已删除或停止公示：%s" % document
                        else:
                            changed_fields = sorted(
                                key for key in set(before or {}) | set(after or {})
                                if not str(key).startswith("_") and (before or {}).get(key) != (after or {}).get(key)
                            )
                            summary = "行政处罚内容变更：%s%s" % (
                                document,
                                "（%s）" % "、".join(changed_fields[:6]) if changed_fields else "",
                            )
                        event_rows.append(
                            (
                                record.name,
                                "行政处罚",
                                1,
                                summary,
                                change_type,
                                identity,
                                _canonical_json(before) if before else "",
                                _canonical_json(after) if after else "",
                                previous_evidence.get(identity, ""),
                                (
                                    str(evidence_items.get(identity, {}).get("screenshot_path", ""))
                                    if change_type != "deleted"
                                    else overview_evidence
                                ),
                                now,
                            )
                        )
                connection.executemany(
                    """
                    INSERT INTO announcements
                      (company_name, section_name, change_count, summary, change_type,
                       record_key, before_json, after_json, before_evidence,
                       after_evidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    event_rows,
                )
            connection.commit()
            return new_counts if had_previous else {}
        finally:
            connection.close()

    def list_announcements(self, limit: int = 100) -> List[Dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM announcements ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "company": str(row["company_name"]),
                    "section": str(row["section_name"]),
                    "count": int(row["change_count"]),
                    "summary": str(row["summary"]),
                    "type": str(row["change_type"]),
                    "recordKey": str(row["record_key"]),
                    "hasBeforeEvidence": bool(row["before_evidence"]),
                    "hasAfterEvidence": bool(row["after_evidence"]),
                    "createdAt": datetime.fromtimestamp(float(row["created_at"])).strftime("%Y-%m-%d %H:%M"),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def announcement_evidence_path(self, announcement_id: int, which: str) -> Optional[Path]:
        column = "before_evidence" if which == "before" else "after_evidence"
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT %s AS evidence_path FROM announcements WHERE id = ?" % column,
                (announcement_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not str(row["evidence_path"]):
            return None
        path = Path(str(row["evidence_path"])).resolve()
        return path if path.is_file() else None

    def announcement_record(self, announcement_id: int) -> Optional[Dict[str, Any]]:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM announcements WHERE id = ?",
                (announcement_id,),
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row is not None else None

    @staticmethod
    def _serialize_evidence_capture(
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> Dict[str, Any]:
        capture_id = int(row["id"])
        item_rows = connection.execute(
            """
            SELECT id, identity_key, screenshot_path
            FROM penalty_evidence
            WHERE capture_id = ?
            ORDER BY id
            """,
            (capture_id,),
        ).fetchall()
        return {
            "id": capture_id,
            "company": str(row["company_name"]),
            "capturedAt": datetime.fromtimestamp(float(row["captured_at"])).astimezone().isoformat(timespec="seconds"),
            "penaltyCount": int(row["penalty_count"]),
            "sourceUrl": str(row["source_url"]),
            "hasOverview": bool(row["overview_path"]) and Path(str(row["overview_path"])).is_file(),
            "hasPanel": bool(row["panel_path"]) and Path(str(row["panel_path"])).is_file(),
            "hasHtml": bool(row["html_path"]) and Path(str(row["html_path"])).is_file(),
            "hasMetadata": bool(row["metadata_path"]) and Path(str(row["metadata_path"])).is_file(),
            "items": [
                {
                    "id": int(item["id"]),
                    "identity": str(item["identity_key"]),
                    "documentNumber": str(item["identity_key"]),
                    "hasImage": bool(item["screenshot_path"])
                    and Path(str(item["screenshot_path"])).is_file(),
                }
                for item in item_rows
            ],
        }

    def list_company_evidence(self, company_name: str, limit: int = 50) -> List[Dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM evidence_captures
                WHERE company_name = ?
                ORDER BY captured_at DESC, id DESC
                LIMIT ?
                """,
                (company_name, max(1, min(limit, 200))),
            ).fetchall()
            return [self._serialize_evidence_capture(connection, row) for row in rows]
        finally:
            connection.close()

    def evidence_capture_record(self, capture_id: int) -> Optional[Dict[str, Any]]:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM evidence_captures WHERE id = ?",
                (capture_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["items"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM penalty_evidence WHERE capture_id = ? ORDER BY id",
                    (capture_id,),
                ).fetchall()
            ]
            return result
        finally:
            connection.close()

    def evidence_asset_path(self, capture_id: int, asset: str) -> Optional[Path]:
        columns = {
            "overview": "overview_path",
            "panel": "panel_path",
            "html": "html_path",
            "metadata": "metadata_path",
        }
        column = columns.get(asset)
        if column is None:
            return None
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT %s AS path FROM evidence_captures WHERE id = ?" % column,
                (capture_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not str(row["path"]):
            return None
        path = Path(str(row["path"])).resolve()
        return path if path.is_file() else None

    def evidence_item_path(self, capture_id: int, item_id: int) -> Optional[Path]:
        connection = self.connect()
        try:
            row = connection.execute(
                """
                SELECT screenshot_path AS path FROM penalty_evidence
                WHERE id = ? AND capture_id = ?
                """,
                (item_id, capture_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not str(row["path"]):
            return None
        path = Path(str(row["path"])).resolve()
        return path if path.is_file() else None

    def index_evidence_directory(self, output_dir: Path) -> int:
        """把已有证据目录补录为公司级证据批次。

        这使升级前已落盘的截图也能立即在网页端查看，
        也覆盖没有任何处罚条目、但仍有整页证据的企业。
        """

        evidence_root = Path(output_dir) / "evidence"
        if not evidence_root.is_dir():
            return 0
        indexed = 0
        for metadata_path in evidence_root.rglob("行政处罚-证据清单.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                company_name = str(metadata.get("company_name", "")).strip()
                if not company_name:
                    continue
                captured_raw = str(metadata.get("captured_at", ""))
                try:
                    captured_at = datetime.fromisoformat(captured_raw).timestamp()
                except ValueError:
                    captured_at = metadata_path.stat().st_mtime
                resolved_metadata = str(metadata_path.resolve())
                connection = self.connect()
                try:
                    existing = connection.execute(
                        "SELECT id FROM evidence_captures WHERE metadata_path = ?",
                        (resolved_metadata,),
                    ).fetchone()
                    if existing is None:
                        cursor = connection.execute(
                            """
                            INSERT INTO evidence_captures
                              (company_name, penalty_count, source_url, overview_path,
                               panel_path, html_path, metadata_path, overview_sha256,
                               captured_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                company_name,
                                int(metadata.get("penalty_count_page", metadata.get("penalty_count_json", 0))),
                                str(metadata.get("source_url", "")),
                                str(metadata.get("overview_path", "")),
                                str(metadata.get("penalty_panel_path", "")),
                                str(metadata.get("html_path", "")),
                                resolved_metadata,
                                str(metadata.get("overview_sha256", "")),
                                captured_at,
                            ),
                        )
                        capture_id = int(cursor.lastrowid)
                        indexed += 1
                    else:
                        capture_id = int(existing["id"])
                    for item in metadata.get("items") or []:
                        identity = str(item.get("identity") or item.get("document_number") or "").strip()
                        screenshot_path = str(item.get("screenshot_path", ""))
                        if not identity:
                            continue
                        legacy = connection.execute(
                            """
                            SELECT id FROM penalty_evidence
                            WHERE company_name = ? AND screenshot_path = ?
                            ORDER BY id DESC LIMIT 1
                            """,
                            (company_name, screenshot_path),
                        ).fetchone()
                        if legacy is not None:
                            connection.execute(
                                "UPDATE penalty_evidence SET capture_id = ? WHERE id = ?",
                                (capture_id, int(legacy["id"])),
                            )
                        else:
                            connection.execute(
                                """
                                INSERT INTO penalty_evidence
                                  (capture_id, company_name, identity_key, fingerprint,
                                   screenshot_path, overview_path, metadata_path,
                                   screenshot_sha256, captured_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    capture_id,
                                    company_name,
                                    identity,
                                    hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                                    screenshot_path,
                                    str(metadata.get("overview_path", "")),
                                    resolved_metadata,
                                    str(item.get("sha256", "")),
                                    captured_at,
                                ),
                            )
                    connection.commit()
                finally:
                    connection.close()
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        # 早期版本在 JSON/页面条数不一致时，已经写下整页图和
        # HTML，但会在生成证据清单前退出。这些部分证据也应
        # 可以从公司详情中调用。
        for overview_path in evidence_root.rglob("行政处罚-整页.png"):
            resolved_overview = str(overview_path.resolve())
            connection = self.connect()
            try:
                existing = connection.execute(
                    "SELECT id FROM evidence_captures WHERE overview_path = ?",
                    (resolved_overview,),
                ).fetchone()
                if existing is not None:
                    continue
                batch_dir = overview_path.parent
                company_name = batch_dir.parent.name
                if not company_name:
                    continue
                panel_path = batch_dir / "行政处罚-全部条目.png"
                html_path = batch_dir / "行政处罚-页面源码.html"
                try:
                    captured_at = datetime.strptime(
                        "%s%s" % (batch_dir.parent.parent.name, batch_dir.name),
                        "%Y-%m-%d%H%M%S",
                    ).astimezone().timestamp()
                except ValueError:
                    captured_at = overview_path.stat().st_mtime
                item_paths = sorted(batch_dir.glob("行政处罚-[0-9][0-9]-*.png"))
                cursor = connection.execute(
                    """
                    INSERT INTO evidence_captures
                      (company_name, penalty_count, overview_path, panel_path,
                       html_path, captured_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company_name,
                        len(item_paths),
                        resolved_overview,
                        str(panel_path.resolve()) if panel_path.is_file() else "",
                        str(html_path.resolve()) if html_path.is_file() else "",
                        captured_at,
                    ),
                )
                capture_id = int(cursor.lastrowid)
                for item_path in item_paths:
                    identity = item_path.stem
                    connection.execute(
                        """
                        INSERT INTO penalty_evidence
                          (capture_id, company_name, identity_key, fingerprint,
                           screenshot_path, overview_path, screenshot_sha256,
                           captured_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            capture_id,
                            company_name,
                            identity,
                            hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                            str(item_path.resolve()),
                            resolved_overview,
                            hashlib.sha256(item_path.read_bytes()).hexdigest(),
                            captured_at,
                        ),
                    )
                connection.commit()
                indexed += 1
            except (OSError, sqlite3.DatabaseError):
                continue
            finally:
                connection.close()
        return indexed

    def create_daily_monitor_task(self, companies: Sequence[str], run_date: str) -> Optional[Dict[str, Any]]:
        connection = self.connect()
        try:
            existing = connection.execute(
                "SELECT task_id FROM monitor_runs WHERE run_date = ?",
                (run_date,),
            ).fetchone()
            if existing is not None:
                return None
        finally:
            connection.close()
        task = self.create_task("每日行政管理监控 · %s" % run_date, companies)
        connection = self.connect()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO monitor_runs(run_date, task_id, created_at) VALUES (?, ?, ?)",
                (run_date, task["id"], _utc_timestamp()),
            )
            connection.commit()
        finally:
            connection.close()
        return task

    def latest_record(self, company_name: str) -> Optional[EnterpriseRecord]:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT payload_json FROM latest_records WHERE company_name = ?",
                (company_name,),
            ).fetchone()
            if row is None:
                return None
            return _record_from_payload(json.loads(str(row["payload_json"])))
        finally:
            connection.close()

    def list_companies(self) -> List[Dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT company_name, payload_json, collected_at FROM latest_records ORDER BY collected_at DESC"
            ).fetchall()
        finally:
            connection.close()
        result = []
        for index, row in enumerate(rows, start=1):
            record = _record_from_payload(json.loads(str(row["payload_json"])))
            basic = record.basic
            address = str(basic.get("地址", ""))
            result.append(
                {
                    "id": index,
                    "name": record.name,
                    "code": str(basic.get("统一社会信用代码", "")),
                    "legalPerson": str(basic.get("法人", basic.get("法定代表人", ""))),
                    "status": str(basic.get("企业状态", "")) or "未知",
                    "permission": len(record.permissions),
                    "penalty": len(record.penalties),
                    "updated": datetime.fromtimestamp(float(row["collected_at"])).strftime("%Y-%m-%d %H:%M"),
                    "region": address[:2] if address else "--",
                }
            )
        return result

    def save_credit_score(self, company_name: str, payload: Mapping[str, Any]) -> None:
        raw_score = payload.get("scoreTotal")
        try:
            score_total = float(raw_score) if raw_score not in (None, "", "--") else None
        except (TypeError, ValueError):
            score_total = None
        collected_at = _utc_timestamp()
        connection = self.connect()
        try:
            connection.execute(
                """
                INSERT INTO credit_scores
                  (company_name, payload_json, score_total, report_date, collected_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(company_name) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  score_total = excluded.score_total,
                  report_date = excluded.report_date,
                  collected_at = excluded.collected_at
                """,
                (
                    company_name,
                    json.dumps(dict(payload), ensure_ascii=False, default=str),
                    score_total,
                    str(payload.get("creditreportEnddate") or ""),
                    collected_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _serialize_credit_score(row: Mapping[str, Any]) -> Dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload["scoreTotal"] = row["score_total"]
        payload["reportDate"] = str(row["report_date"])
        payload["collectedAt"] = datetime.fromtimestamp(
            float(row["collected_at"])
        ).strftime("%Y-%m-%d %H:%M")
        return payload

    def credit_score(self, company_name: str) -> Optional[Dict[str, Any]]:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM credit_scores WHERE company_name = ?",
                (company_name,),
            ).fetchone()
            return self._serialize_credit_score(row) if row is not None else None
        finally:
            connection.close()

    def list_credit_scores(self) -> Dict[str, Dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute("SELECT * FROM credit_scores").fetchall()
            return {
                str(row["company_name"]): self._serialize_credit_score(row)
                for row in rows
            }
        finally:
            connection.close()

    @staticmethod
    def serialize_credit_score_job(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "name": "采集上海住建信用分",
            "status": str(row["status"]),
            "progress": int(row["progress"]),
            "completed": int(row["completed"]),
            "total": int(row["total"]),
            "currentCompany": str(row["current_company"]),
            "error": str(row["error"]),
            "created": datetime.fromtimestamp(float(row["created_at"])).strftime("%H:%M"),
        }

    def create_credit_score_job(self, companies: Sequence[str]) -> Dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = _utc_timestamp()
        connection = self.connect()
        try:
            connection.execute(
                """
                INSERT INTO credit_score_jobs
                  (id, company_names, status, progress, completed, total,
                   current_company, error, created_at, updated_at)
                VALUES (?, ?, 'queued', 0, 0, ?, '', '', ?, ?)
                """,
                (
                    job_id,
                    json.dumps(list(companies), ensure_ascii=False),
                    len(companies),
                    now,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM credit_score_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return self.serialize_credit_score_job(row)
        finally:
            connection.close()

    def raw_credit_score_job(self, job_id: str) -> Optional[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM credit_score_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        finally:
            connection.close()

    def update_credit_score_job(self, job_id: str, **values: Any) -> None:
        allowed = {
            "status", "progress", "completed", "current_company", "error"
        }
        selected = {key: value for key, value in values.items() if key in allowed}
        if not selected:
            return
        selected["updated_at"] = _utc_timestamp()
        columns = ", ".join("%s = ?" % key for key in selected)
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE credit_score_jobs SET %s WHERE id = ?" % columns,
                tuple(selected.values()) + (job_id,),
            )
            connection.commit()
        finally:
            connection.close()

    def latest_credit_score_job(self) -> Optional[Dict[str, Any]]:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM credit_score_jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return self.serialize_credit_score_job(row) if row is not None else None
        finally:
            connection.close()

    def active_credit_score_job(self) -> Optional[Dict[str, Any]]:
        connection = self.connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM credit_score_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            return self.serialize_credit_score_job(row) if row is not None else None
        finally:
            connection.close()

    def recover_credit_score_jobs(self) -> List[str]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """
                SELECT id FROM credit_score_jobs
                WHERE status IN ('queued', 'running') ORDER BY created_at
                """
            ).fetchall()
            connection.execute(
                """
                UPDATE credit_score_jobs SET status = 'queued', current_company = ''
                WHERE status = 'running'
                """
            )
            connection.commit()
            return [str(row["id"]) for row in rows]
        finally:
            connection.close()

    def history_rows(self, section: Optional[str] = None, company_name: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if section:
            clauses.append("section_name = ?")
            params.append(section)
        if company_name:
            clauses.append("company_name = ?")
            params.append(company_name)
        sql = "SELECT * FROM history_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY section_name, company_name, first_seen_at, id"
        connection = self.connect()
        try:
            rows = connection.execute(sql, params).fetchall()
            return [
                {
                    "company_name": row["company_name"],
                    "section_name": row["section_name"],
                    "record_key": row["record_key"],
                    "raw_json": row["raw_json"],
                    "first_seen_at": datetime.fromtimestamp(float(row["first_seen_at"])),
                    "last_seen_at": datetime.fromtimestamp(float(row["last_seen_at"])),
                    "seen_count": row["seen_count"],
                }
                for row in rows
            ]
        finally:
            connection.close()


class CrawlManager:
    """单 Chrome 会话后台队列，进度只由真实采集阶段驱动。"""

    def __init__(
        self,
        store: TaskStore,
        output_dir: Path,
        crawler_factory: Optional[Callable[[], tuple[Any, CreditChinaCrawler]]] = None,
    ) -> None:
        self.store = store
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="API真实采集队列", daemon=True)
        self._crawler_factory = crawler_factory or self._new_crawler
        # 企业之间不另外停顿；同一 IP 内的官网请求由 BrowserClient
        # 按 request_interval 限速。
        self.company_interval = 0.0
        self.request_interval = max(
            0.0,
            float(os.getenv("CREDITCHINA_REQUEST_INTERVAL_SECONDS", "1")),
        )
        self.dynamic_proxy_enabled = kuaidaili_enabled()
        self.max_proxy_replacements = max(
            0,
            int(os.getenv("KDL_MAX_PROXY_REPLACEMENTS_PER_TASK", "20")),
        )
        self.proxy_replacement_cooldown = max(
            0.0,
            float(os.getenv("KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS", "1")),
        )
        self._started = False
        self._active_lock = threading.Lock()
        self._active_client: Any = None
        self._cancelled_task_ids: set[str] = set()

    @staticmethod
    def _new_crawler() -> tuple[BrowserClient, CreditChinaCrawler]:
        api_config = ApiConfig.from_env(
            page_size=int(os.getenv("CREDITCHINA_PAGE_SIZE", "10")),
            max_pages=int(os.getenv("CREDITCHINA_MAX_PAGES", "20")),
            mode=os.getenv("CREDITCHINA_API_MODE", "current"),
        )
        provider = KuaidailiPrivateProxyProvider.from_env()
        lease = provider.extract_one() if provider is not None else None
        proxies = (lease.spec,) if lease is not None else proxies_from_values(None)
        http_config = HttpConfig(
            transport="requests",
            timeout=float(os.getenv("CREDITCHINA_TIMEOUT", "10")),
            retries=int(os.getenv("CREDITCHINA_RETRIES", "5")),
            backoff=float(os.getenv("CREDITCHINA_BACKOFF", "0.8")),
            proxies=proxies,
            cookie=os.getenv("CREDITCHINA_COOKIE", ""),
            jfbym_token=os.getenv("JFBYM_TOKEN", ""),
            jfbym_type=os.getenv("JFBYM_TYPE", "10103"),
        )
        client = BrowserClient(
            http_config,
            api_config,
            captcha_timeout=float(os.getenv("CREDITCHINA_CAPTCHA_TIMEOUT", "300")),
            request_interval=float(os.getenv("CREDITCHINA_REQUEST_INTERVAL_SECONDS", "1")),
            allow_manual_captcha=False,
            captcha_auto_attempts=int(os.getenv("CREDITCHINA_CAPTCHA_AUTO_ATTEMPTS", "3")),
            captcha_solver_timeout=float(
                os.getenv("CREDITCHINA_CAPTCHA_SOLVER_TIMEOUT", "30")
            ),
        )
        client.proxy_label = "快代理 %s" % lease.masked_label if lease is not None else ("静态代理" if proxies else "直连")
        return client, CreditChinaCrawler(client, api_config)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        for task_id in self.store.recover_tasks():
            self._queue.put(task_id)

    def stop(self) -> None:
        if not self._started:
            return
        self._queue.put(None)
        self._thread.join(timeout=5)

    def enqueue(self, task_id: str) -> None:
        self._queue.put(task_id)

    def cancel(self, task_ids: Sequence[str]) -> None:
        with self._active_lock:
            self._cancelled_task_ids.update(str(task_id) for task_id in task_ids)
            client = self._active_client
        if client is not None:
            if hasattr(client, "abort"):
                client.abort()
            elif hasattr(client, "close"):
                client.close()

    def _is_cancelled(self, task_id: str) -> bool:
        with self._active_lock:
            if task_id in self._cancelled_task_ids:
                return True
        row = self.store.raw_task(task_id)
        return row is None or str(row["status"]) == "cancelled"

    def _wait_if_paused(self, task_id: str) -> bool:
        while True:
            row = self.store.raw_task(task_id)
            if row is None or str(row["status"]) == "cancelled":
                return False
            status = str(row["status"])
            if status == "pause_requested":
                self.store.update_task(task_id, status="paused", speed="--")
            if status not in ("paused", "pause_requested"):
                return True
            time.sleep(0.5)

    def _run(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                if task_id is None:
                    return
                self._execute(task_id)
            finally:
                self._queue.task_done()

    def _execute(self, task_id: str) -> None:
        row = self.store.raw_task(task_id)
        if row is None or str(row["status"]) == "cancelled":
            return
        companies = json.loads(str(row["company_names"]))
        client: Any = None
        crawler: Any = None
        partial_errors: List[str] = []
        proxy_replacements = 0
        try:
            if self._is_cancelled(task_id):
                return
            self.store.update_task(task_id, status="running", progress=max(1, int(row["progress"])), speed="真实采集中", error="")
            file_storage = FileStorage(self.output_dir, write_json=True, write_text=True, write_xlsx=True)
            total = len(companies)
            # completed 是已完整落盘的公司断点。任务恢复或服务重启时
            # 直接从下一家开始，不重复查询已完成公司。
            start_index = max(0, min(int(row["completed"]), total))
            checkpoint_company = str(row["checkpoint_company"] or "")
            checkpoint_payload = str(row["checkpoint_payload"] or "")
            for index in range(start_index, total):
                company = companies[index]
                # 官网数据和证据截图是两个独立阶段。如果数据已经成功获取，
                # 而进入详情页截图时当前 IP 被风控，保留内存中的 record；
                # 换 IP 后只重试截图，避免重复请求全部接口后又在截图前触发限流。
                record: Optional[EnterpriseRecord] = None
                if checkpoint_company == company and checkpoint_payload:
                    try:
                        record = _record_from_payload(json.loads(checkpoint_payload))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        checkpoint_company = ""
                        checkpoint_payload = ""
                        self.store.update_task(
                            task_id,
                            checkpoint_company="",
                            checkpoint_payload="",
                        )
                evidence: Optional[Dict[str, Any]] = None
                while True:
                    if not self._wait_if_paused(task_id):
                        return
                    try:
                        if client is None or crawler is None:
                            self.store.update_task(
                                task_id,
                                speed=("正在提取 1 个快代理 IP" if self.dynamic_proxy_enabled else "正在建立采集会话"),
                                current_company=company,
                            )
                            client, crawler = self._crawler_factory()
                            with self._active_lock:
                                self._active_client = client
                            if self._is_cancelled(task_id):
                                return
                        phase_start = int((index / total) * 100)
                        proxy_label = str(getattr(client, "proxy_label", "直连"))
                        self.store.update_task(
                            task_id,
                            status="running",
                            progress=max(1, phase_start + 2),
                            completed=index,
                            speed="正在请求官网 · %s" % proxy_label,
                            current_company=company,
                        )
                        if record is None:
                            if hasattr(crawler, "crawl_administration_company"):
                                record = crawler.crawl_administration_company(company)
                            else:
                                record = crawler.crawl_company(company)
                            # 企业接口阶段完成就写入持久断点。即使程序在证据
                            # 截图阶段退出，下次恢复也只会继续截图。
                            checkpoint_company = company
                            checkpoint_payload = json.dumps(
                                _record_payload(record),
                                ensure_ascii=False,
                                default=str,
                            )
                            self.store.update_task(
                                task_id,
                                checkpoint_company=checkpoint_company,
                                checkpoint_payload=checkpoint_payload,
                            )
                        if self._is_cancelled(task_id):
                            return
                        if evidence is None and hasattr(client, "capture_penalty_evidence"):
                            try:
                                self.store.update_task(task_id, speed="正在保存官网处罚截图 · %s" % proxy_label)
                                evidence = client.capture_penalty_evidence(
                                    record.name,
                                    str(record.basic.get("统一社会信用代码", "")),
                                    record.encry_str,
                                    record.penalties,
                                    self.output_dir,
                                )
                                consistency_error = str(evidence.get("consistency_error", "")).strip()
                                if consistency_error:
                                    record.errors["证据一致性"] = consistency_error
                            except (AccessIntercepted, ProxyUnavailable):
                                raise
                            except Exception as exc:
                                record.errors["证据截图"] = str(exc)
                        if record.errors:
                            partial_errors.append(
                                "%s：%s" % (company, "、".join(record.errors.keys()))
                            )
                        if self._is_cancelled(task_id):
                            return
                        self.store.update_task(
                            task_id,
                            progress=min(98, int(((index + 0.85) / total) * 100)),
                            speed="正在写入数据",
                        )
                        file_storage.save(record)
                        self.store.save_record(record, evidence=evidence)
                        completed = index + 1
                        self.store.update_task(
                            task_id,
                            progress=int((completed / total) * 100),
                            completed=completed,
                            speed="真实采集中" if completed < total else "--",
                            checkpoint_company="",
                            checkpoint_payload="",
                        )
                        checkpoint_company = ""
                        checkpoint_payload = ""
                        break
                    except (ProxyUnavailable, AccessIntercepted) as exc:
                        if self._is_cancelled(task_id):
                            return
                        if client is not None and hasattr(client, "close"):
                            client.close()
                        client = None
                        crawler = None
                        with self._active_lock:
                            self._active_client = None
                        if not self.dynamic_proxy_enabled:
                            raise
                        proxy_replacements += 1
                        if proxy_replacements > self.max_proxy_replacements:
                            if isinstance(exc, AccessIntercepted):
                                raise AccessIntercepted(
                                    "官网连续风控，已更换 %d 个 IP 仍未恢复：%s"
                                    % (self.max_proxy_replacements, str(exc))
                                ) from exc
                            raise RequestFailed(
                                "代理连续不可用，已达到本轮最大更换次数 %d：%s"
                                % (self.max_proxy_replacements, str(exc))
                            ) from exc
                        reason = (
                            "官网风控/限流，正在销毁当前会话并更换 IP"
                            if isinstance(exc, AccessIntercepted)
                            else "当前代理不可用，正在申请新 IP"
                        )
                        self.store.update_task(
                            task_id,
                            speed="%s（%d/%d）"
                            % (reason, proxy_replacements, self.max_proxy_replacements),
                            current_company=company,
                        )
                        deadline = time.monotonic() + self.proxy_replacement_cooldown
                        while time.monotonic() < deadline:
                            if not self._wait_if_paused(task_id):
                                return
                            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            self.store.update_task(
                task_id,
                status="partial" if partial_errors else "completed",
                progress=100,
                completed=total,
                speed="--",
                current_company="",
                error=("部分栏目采集失败：" + "；".join(partial_errors)) if partial_errors else "",
                checkpoint_company="",
                checkpoint_payload="",
            )
        except AccessIntercepted as exc:
            if self._is_cancelled(task_id):
                return
            current = self.store.raw_task(task_id)
            completed = int(current["completed"]) if current is not None else 0
            total = int(current["total"]) if current is not None else len(companies)
            stop_reason = (
                "已用尽本轮换 IP 重试次数，官网风控仍未恢复，本轮停止；"
                if self.dynamic_proxy_enabled
                else "已检测到官网访问风控，本轮停止；"
            )
            self.store.update_task(
                task_id,
                status="intercepted",
                speed="--",
                current_company="",
                error=(
                    stop_reason
                    +
                    "已成功保留 %d/%d 家的采集结果。%s"
                    % (completed, total, str(exc))
                ),
            )
        except Exception as exc:
            if self._is_cancelled(task_id):
                return
            self.store.update_task(
                task_id,
                status="failed",
                speed="--",
                error=str(exc),
            )
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
            with self._active_lock:
                if self._active_client is client:
                    self._active_client = None


class CreditScoreManager:
    """独立执行上海住建信用分采集，不修改信用中国现有记录。"""

    def __init__(
        self,
        store: TaskStore,
        spider_factory: Optional[Callable[[], ShZjwSpider]] = None,
    ) -> None:
        self.store = store
        self._spider_factory = spider_factory or self._new_spider
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="上海住建信用分队列",
            daemon=True,
        )
        self._started = False
        self._active_lock = threading.Lock()
        self._active_spider: Any = None
        self._cancelled_job_ids: set[str] = set()

    @staticmethod
    def _new_spider() -> ShZjwSpider:
        return ShZjwSpider(
            timeout=float(os.getenv("SH_ZJW_TIMEOUT", "15")),
            proxy=os.getenv("SH_ZJW_PROXY", "").strip() or None,
        )

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        for job_id in self.store.recover_credit_score_jobs():
            self._queue.put(job_id)

    def stop(self) -> None:
        if not self._started:
            return
        self._queue.put(None)
        self._thread.join(timeout=5)

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def cancel(self, job_ids: Sequence[str]) -> None:
        with self._active_lock:
            self._cancelled_job_ids.update(str(job_id) for job_id in job_ids)
            spider = self._active_spider
        if spider is not None and hasattr(spider, "close"):
            spider.close()

    def _is_cancelled(self, job_id: str) -> bool:
        with self._active_lock:
            if job_id in self._cancelled_job_ids:
                return True
        row = self.store.raw_credit_score_job(job_id)
        return row is None or str(row["status"]) == "cancelled"

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                self._execute(job_id)
            finally:
                self._queue.task_done()

    def _execute(self, job_id: str) -> None:
        row = self.store.raw_credit_score_job(job_id)
        if row is None or str(row["status"]) == "cancelled":
            return
        companies = list(json.loads(str(row["company_names"])))
        total = len(companies)
        failures: List[str] = []
        spider = self._spider_factory()
        with self._active_lock:
            self._active_spider = spider
        try:
            if self._is_cancelled(job_id):
                return
            start_index = max(0, min(int(row["completed"]), total))
            self.store.update_credit_score_job(
                job_id,
                status="running",
                progress=int((start_index / total) * 100) if total else 100,
                error="",
            )
            for index in range(start_index, total):
                if self._is_cancelled(job_id):
                    return
                company_name = str(companies[index])
                self.store.update_credit_score_job(
                    job_id,
                    current_company=company_name,
                    progress=int((index / total) * 100) if total else 100,
                )
                existing = self.store.latest_record(company_name)
                company_code = ""
                if existing is not None:
                    company_code = str(
                        existing.basic.get("统一社会信用代码", "")
                    ).strip()
                if not company_code:
                    company_code = default_company_code(company_name)
                try:
                    payload = spider.crawl(company_name, company_code)
                    if self._is_cancelled(job_id):
                        return
                    if payload is None:
                        failures.append("%s：未查到" % company_name)
                    else:
                        payload["input_name"] = company_name
                        payload["input_code"] = company_code
                        self.store.save_credit_score(company_name, payload)
                except Exception as exc:
                    failures.append("%s：%s" % (company_name, str(exc)))
                completed = index + 1
                self.store.update_credit_score_job(
                    job_id,
                    completed=completed,
                    progress=int((completed / total) * 100) if total else 100,
                )
            self.store.update_credit_score_job(
                job_id,
                status="partial" if failures else "completed",
                progress=100,
                completed=total,
                current_company="",
                error="；".join(failures),
            )
        except Exception as exc:
            if self._is_cancelled(job_id):
                return
            self.store.update_credit_score_job(
                job_id,
                status="failed",
                current_company="",
                error=str(exc),
            )
        finally:
            if hasattr(spider, "close"):
                spider.close()
            with self._active_lock:
                if self._active_spider is spider:
                    self._active_spider = None


class CreateTaskRequest(BaseModel):
    name: str = ""
    companies: List[str] = Field(min_length=1, max_length=500)


class TaskActionRequest(BaseModel):
    action: str


class MonitorCompaniesRequest(BaseModel):
    companies: List[str] = Field(min_length=1, max_length=100)


class EnvSettingsRequest(BaseModel):
    values: Dict[str, str] = Field(default_factory=dict)


class PathSettingsRequest(BaseModel):
    values: Dict[str, str] = Field(default_factory=dict)


"""设置页面可编辑的环境变量。

``sensitive=True`` 的键在 GET 时只返回掩码，前端留空表示保持不变；
带 ``restart=True`` 注释的键保存后需要重启服务才完全生效。
"""
EDITABLE_ENV_KEYS: Sequence[Dict[str, Any]] = (
    {"key": "JFBYM_TOKEN", "label": "云码验证码识别 Token", "sensitive": True,
     "hint": "用于后台自动识别信用中国滑块/点选验证码"},
    {"key": "JFBYM_TYPE", "label": "云码验证码类型", "sensitive": False,
     "hint": "默认 10103（滑块），一般无需修改"},
    {"key": "KDL_DPS_API_URL", "label": "快代理提取地址", "sensitive": False,
     "hint": "私密代理单 IP 提取链接，必须包含 num=1"},
    {"key": "KDL_DPS_SECRET_ID", "label": "快代理 SecretId", "sensitive": True,
     "hint": "快代理 HMAC-SHA1 签名鉴权"},
    {"key": "KDL_DPS_SECRET_KEY", "label": "快代理 SecretKey", "sensitive": True,
     "hint": "与 SecretToken 二选一"},
    {"key": "KDL_DPS_SECRET_TOKEN", "label": "快代理 SecretToken", "sensitive": True,
     "hint": "令牌鉴权方式；与 SecretKey 二选一"},
    {"key": "KDL_DPS_USERNAME", "label": "快代理用户名", "sensitive": True,
     "hint": "用户名密码鉴权时使用"},
    {"key": "KDL_DPS_PASSWORD", "label": "快代理密码", "sensitive": True,
     "hint": "用户名密码鉴权时使用"},
    {"key": "KDL_MAX_PROXY_REPLACEMENTS_PER_TASK", "label": "单任务最大换 IP 次数", "sensitive": False,
     "hint": "官网连续风控时自动更换代理的上限"},
    {"key": "SH_ZJW_PROXY", "label": "上海住建专用代理", "sensitive": False,
     "hint": "信用分接口代理，留空表示直连"},
    {"key": "CREDITCHINA_COOKIE", "label": "信用中国固定 Cookie", "sensitive": True,
     "hint": "仅 requests/urllib 模式使用，一般留空"},
    {"key": "CREDITCHINA_REQUEST_INTERVAL_SECONDS", "label": "官网请求间隔（秒）", "sensitive": False,
     "hint": "同一 IP 对官网请求的最小间隔"},
)

SENSITIVE_ENV_KEYS = {item["key"] for item in EDITABLE_ENV_KEYS if item["sensitive"]}
EDITABLE_ENV_KEY_SET = {item["key"] for item in EDITABLE_ENV_KEYS}

ENV_PATH_KEYS: Sequence[Dict[str, Any]] = (
    {"key": "CREDITCHINA_OUTPUT", "label": "运行结果目录",
     "hint": "SQLite、Excel、官网截图与证据包的保存位置"},
    {"key": "CREDITCHINA_API_STATE", "label": "看板数据库文件",
     "hint": "任务与历史仓 SQLite 路径，默认在输出目录内"},
    {"key": "CREDITCHINA_MONITOR_COMPANIES", "label": "固定企业名单文件",
     "hint": "每行一家企业；相对路径以项目根目录为准"},
)
ENV_PATH_KEY_SET = {item["key"] for item in ENV_PATH_KEYS}

_ENV_MASK = "••••••••"


def _env_file_path() -> Path:
    configured = os.getenv("CREDITCHINA_ENV_PATH", "").strip()
    return Path(configured).expanduser() if configured else PROJECT_ROOT / ".env.local"


def _read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _update_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """就地更新 .env.local：保留注释与顺序，新键追加到文件末尾。"""

    remaining = dict(updates)
    lines: List[str] = []
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    value = remaining.pop(key)
                    lines.append("%s=%s" % (key, value) if value else "%s=" % key)
                    continue
            lines.append(raw_line)
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        for key, value in remaining.items():
            lines.append("%s=%s" % (key, value))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _current_env_value(key: str) -> str:
    """进程内环境变量优先，其次 .env.local 文件。"""

    value = os.environ.get(key)
    if value is not None:
        return value
    return _read_env_file(_env_file_path()).get(key, "")


def load_monitor_companies(path: Path) -> List[str]:
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name = line.strip()
        if name and not name.startswith("#"):
            names.append(name)
    return list(dict.fromkeys(names))


def save_monitor_companies(path: Path, companies: Sequence[str]) -> List[str]:
    names = list(dict.fromkeys(name.strip() for name in companies if name.strip()))
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# 固定监控企业名单：每行一家，可通过看板继续动态添加。\n"
        "# 空行和以 # 开头的说明会被忽略，重复企业会自动去重。\n"
        + "\n".join(names)
        + ("\n" if names else "")
    )
    path.write_text(content, encoding="utf-8")
    return names


class DailyMonitorScheduler:
    """每天为静态企业池创建一次行政管理采集任务。"""

    def __init__(self, store: TaskStore, manager: CrawlManager, company_file: Path) -> None:
        self.store = store
        self.manager = manager
        self.company_file = company_file
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="每日行政管理调度", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def ensure_today(self) -> Optional[Dict[str, Any]]:
        companies = load_monitor_companies(self.company_file)
        if not companies:
            return None
        task = self.store.create_daily_monitor_task(companies, datetime.now().strftime("%Y-%m-%d"))
        if task:
            self.manager.enqueue(str(task["id"]))
        return task

    def _run(self) -> None:
        while not self._stop.is_set():
            self.ensure_today()
            self._stop.wait(30)

    @staticmethod
    def next_run_text() -> str:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.strftime("%Y-%m-%d %H:%M")


def _remove_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def create_app(
    state_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    crawler_factory: Optional[Callable[[], tuple[Any, CreditChinaCrawler]]] = None,
    credit_spider_factory: Optional[Callable[[], ShZjwSpider]] = None,
) -> FastAPI:
    state_path = state_path or _project_path_from_env("CREDITCHINA_API_STATE", DEFAULT_STATE_PATH)
    output_dir = output_dir or _project_path_from_env("CREDITCHINA_OUTPUT", DEFAULT_OUTPUT_DIR)
    store = TaskStore(state_path)
    manager = CrawlManager(
        store,
        output_dir,
        crawler_factory=crawler_factory,
    )
    credit_score_manager = CreditScoreManager(
        store,
        spider_factory=credit_spider_factory,
    )
    store.index_evidence_directory(manager.output_dir)
    company_file = _project_path_from_env(
        "CREDITCHINA_MONITOR_COMPANIES", DEFAULT_COMPANY_FILE
    )
    company_file_lock = threading.Lock()
    env_file_lock = threading.Lock()
    scheduler = DailyMonitorScheduler(store, manager, company_file)
    auto_daily = os.getenv("CREDITCHINA_AUTO_DAILY", "0").strip().lower() in ("1", "true", "yes", "on")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        manager.start()
        credit_score_manager.start()
        if auto_daily:
            scheduler.start()
        yield
        if auto_daily:
            scheduler.stop()
        credit_score_manager.stop()
        manager.stop()

    app = FastAPI(
        title="中建八局 AI 探员 · 信用中国采集 API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        # The settings page saves credentials and storage paths with PUT.
        # Browsers preflight cross-port requests, so omitting PUT here makes a
        # healthy local API look unreachable to the frontend.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
        expose_headers=["Content-Disposition"],
    )

    @app.get("/api/v1/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "creditchina-real-crawler",
            "tasks": len(store.list_tasks()),
            "companies": len(store.list_companies()),
        }

    @app.get("/api/v1/tasks")
    def list_tasks() -> Dict[str, Any]:
        return {"tasks": store.list_tasks()}

    @app.post("/api/v1/tasks", status_code=201)
    def create_task(payload: CreateTaskRequest) -> Dict[str, Any]:
        companies = list(dict.fromkeys(name.strip() for name in payload.companies if name.strip()))
        if not companies:
            raise HTTPException(status_code=400, detail="请至少输入一家企业")
        task = store.create_task(payload.name, companies)
        manager.enqueue(str(task["id"]))
        return {"task": task}

    @app.patch("/api/v1/tasks/{task_id}")
    def task_action(task_id: str, payload: TaskActionRequest) -> Dict[str, Any]:
        if payload.action not in ("pause", "resume"):
            raise HTTPException(status_code=400, detail="不支持的任务操作")
        try:
            task = store.set_action(task_id, payload.action)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if payload.action == "resume":
            manager.enqueue(task_id)
        return {"task": task}

    @app.delete("/api/v1/tasks/{task_id}")
    def delete_task(task_id: str) -> Dict[str, bool]:
        row = store.raw_task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if str(row["status"]) == "running":
            store.update_task(task_id, status="cancelled", speed="--")
        else:
            store.delete_task(task_id)
        return {"deleted": True}

    @app.post("/api/v1/monitor/stop")
    def stop_all_crawlers() -> Dict[str, Any]:
        """停止当前全部行政采集、截图浏览器和信用分任务。"""

        cancelled = store.cancel_all_work()
        manager.cancel(cancelled["tasks"])
        credit_score_manager.cancel(cancelled["creditScoreJobs"])
        return {
            "stopped": bool(cancelled["tasks"] or cancelled["creditScoreJobs"]),
            "taskCount": len(cancelled["tasks"]),
            "creditScoreCount": len(cancelled["creditScoreJobs"]),
        }

    @app.get("/api/v1/companies")
    def companies() -> Dict[str, Any]:
        return {"companies": store.list_companies()}

    @app.get("/api/v1/monitor/dashboard")
    def monitor_dashboard() -> Dict[str, Any]:
        configured = load_monitor_companies(company_file)
        all_company_rows = store.list_companies()
        credit_scores = store.list_credit_scores()
        if configured:
            collected_by_name = {row["name"]: row for row in all_company_rows}
            company_rows = []
            for index, name in enumerate(configured, start=1):
                company_rows.append(collected_by_name.get(name) or {
                    "id": index,
                    "name": name,
                    "code": "",
                    "legalPerson": "",
                    "status": "待采集",
                    "permission": 0,
                    "penalty": 0,
                    "updated": "--",
                    "region": "--",
                })
            collected_count = sum(1 for name in configured if name in collected_by_name)
        else:
            company_rows = all_company_rows
            collected_count = len(company_rows)
        for company_row in company_rows:
            score = credit_scores.get(str(company_row["name"]))
            company_row["creditScore"] = score.get("scoreTotal") if score else None
            company_row["creditScoreDate"] = score.get("reportDate", "") if score else ""
            company_row["creditScoreUpdated"] = score.get("collectedAt", "") if score else ""
        announcements = store.list_announcements(100)
        if configured:
            announcements = [item for item in announcements if item["company"] in configured]
        tasks = store.list_tasks()
        active = next((task for task in tasks if task["status"] in ("queued", "running", "paused", "pause_requested")), None)
        return {
            "scope": "行政管理（行政许可 + 行政处罚）",
            "configuredCompanies": configured,
            "configuredCount": len(configured),
            "collectedCount": collected_count,
            "creditScoreCollectedCount": sum(
                1 for name in configured if name in credit_scores
            ) if configured else len(credit_scores),
            "companies": company_rows,
            "announcements": announcements,
            "activeTask": active,
            "lastTask": tasks[0] if tasks else None,
            "creditScoreTask": store.latest_credit_score_job(),
            "nextRun": scheduler.next_run_text() if auto_daily else "手动触发",
            "autoDaily": auto_daily,
            "intervalSeconds": int(manager.request_interval),
            "proxyMode": (
                "快代理·同 IP 连续采集，风控自动更换"
                if manager.dynamic_proxy_enabled
                else "直连/静态代理"
            ),
            "proxyReplacementLimit": manager.max_proxy_replacements,
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    @app.get("/api/v1/monitor/companies/{company_name}")
    def monitor_company_detail(company_name: str) -> Dict[str, Any]:
        record = store.latest_record(company_name)
        if record is None:
            raise HTTPException(status_code=404, detail="该企业尚未完成首次采集")
        return {
            "name": record.name,
            "basic": record.basic,
            "permissions": record.permissions,
            "penalties": record.penalties,
            "errors": record.errors,
            "evidence": store.list_company_evidence(company_name),
            "creditScore": store.credit_score(company_name),
        }

    @app.get("/api/v1/monitor/companies/{company_name}/evidence")
    def monitor_company_evidence(company_name: str) -> Dict[str, Any]:
        return {
            "company": company_name,
            "captures": store.list_company_evidence(company_name),
        }

    @app.get("/api/v1/monitor/evidence/{capture_id}/assets/{asset}")
    def company_evidence_asset(capture_id: int, asset: str) -> FileResponse:
        if asset not in ("overview", "panel", "html", "metadata"):
            raise HTTPException(status_code=400, detail="不支持的证据文件类型")
        path = store.evidence_asset_path(capture_id, asset)
        if path is None:
            raise HTTPException(status_code=404, detail="该证据文件不存在")
        media_types = {
            "overview": "image/png",
            "panel": "image/png",
            # 这是取证时的 DOM 源码快照，以纯文本展示，避免浏览器
            # 误把其中的相对 CSS/JS 路径请求到本机证据 API。
            "html": "text/plain; charset=utf-8",
            "metadata": "application/json; charset=utf-8",
        }
        return FileResponse(path, media_type=media_types[asset])

    @app.get("/api/v1/monitor/evidence/{capture_id}/items/{item_id}")
    def company_evidence_item(capture_id: int, item_id: int) -> FileResponse:
        path = store.evidence_item_path(capture_id, item_id)
        if path is None:
            raise HTTPException(status_code=404, detail="该行政处罚截图不存在")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/monitor/evidence/{capture_id}/package")
    def company_evidence_package(
        capture_id: int,
        background_tasks: BackgroundTasks,
    ) -> FileResponse:
        capture = store.evidence_capture_record(capture_id)
        if capture is None:
            raise HTTPException(status_code=404, detail="证据批次不存在")
        handle, temporary = tempfile.mkstemp(prefix="creditchina-company-evidence-", suffix=".zip")
        os.close(handle)
        path = Path(temporary)
        manifest = {
            "capture_id": capture_id,
            "company_name": capture["company_name"],
            "captured_at": datetime.fromtimestamp(float(capture["captured_at"])).astimezone().isoformat(),
            "penalty_count": int(capture["penalty_count"]),
            "source_url": capture["source_url"],
        }
        hashes: List[str] = []
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("证据清单/批次说明.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for label, column in (
                ("整页截图", "overview_path"),
                ("处罚栏目", "panel_path"),
                ("页面源码", "html_path"),
                ("官网证据清单", "metadata_path"),
            ):
                raw_path = str(capture.get(column, ""))
                asset_path = Path(raw_path).resolve() if raw_path else None
                if asset_path is None or not asset_path.is_file():
                    continue
                arcname = "%s/%s" % (label, asset_path.name)
                archive.write(asset_path, arcname)
                hashes.append("%s  %s" % (hashlib.sha256(asset_path.read_bytes()).hexdigest(), arcname))
            for index, item in enumerate(capture.get("items") or [], start=1):
                raw_path = str(item.get("screenshot_path", ""))
                item_path = Path(raw_path).resolve() if raw_path else None
                if item_path is None or not item_path.is_file():
                    continue
                arcname = "逐条行政处罚/%02d-%s" % (index, item_path.name)
                archive.write(item_path, arcname)
                hashes.append("%s  %s" % (hashlib.sha256(item_path.read_bytes()).hexdigest(), arcname))
            archive.writestr("证据清单/SHA256SUMS.txt", "\n".join(hashes) + "\n")
        background_tasks.add_task(_remove_file, str(path))
        filename = "%s-%s-行政处罚证据包.zip" % (
            safe_filename(str(capture["company_name"])),
            datetime.fromtimestamp(float(capture["captured_at"])).strftime("%Y%m%d-%H%M%S"),
        )
        return FileResponse(path, media_type="application/zip", filename=filename)

    @app.get("/api/v1/monitor/announcements/{announcement_id}/evidence/{which}")
    def announcement_evidence(announcement_id: int, which: str) -> FileResponse:
        if which not in ("before", "after"):
            raise HTTPException(status_code=400, detail="证据类型只能是 before 或 after")
        path = store.announcement_evidence_path(announcement_id, which)
        if path is None:
            raise HTTPException(status_code=404, detail="该公告没有对应截图")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/monitor/announcements/{announcement_id}/package")
    def announcement_evidence_package(
        announcement_id: int,
        background_tasks: BackgroundTasks,
    ) -> FileResponse:
        record = store.announcement_record(announcement_id)
        if record is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        handle, temporary = tempfile.mkstemp(prefix="creditchina-evidence-", suffix=".zip")
        os.close(handle)
        path = Path(temporary)
        manifest = {
            "announcement_id": announcement_id,
            "company_name": record["company_name"],
            "change_type": record["change_type"],
            "summary": record["summary"],
            "record_key": record["record_key"],
            "detected_at": datetime.fromtimestamp(float(record["created_at"])).astimezone().isoformat(),
            "before": json.loads(record["before_json"]) if record["before_json"] else None,
            "after": json.loads(record["after_json"]) if record["after_json"] else None,
        }
        hashes = []
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("证据清单/事件说明.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for label, raw_path in (
                ("变更前", record["before_evidence"]),
                ("变更后", record["after_evidence"]),
            ):
                if not raw_path:
                    continue
                evidence_path = Path(str(raw_path)).resolve()
                if not evidence_path.is_file():
                    continue
                arcname = "%s/%s" % (label, evidence_path.name)
                archive.write(evidence_path, arcname)
                hashes.append("%s  %s" % (hashlib.sha256(evidence_path.read_bytes()).hexdigest(), arcname))
                metadata_path = evidence_path.parent / "行政处罚-证据清单.json"
                if metadata_path.is_file():
                    metadata_arcname = "%s/%s" % (label, metadata_path.name)
                    archive.write(metadata_path, metadata_arcname)
                    hashes.append("%s  %s" % (hashlib.sha256(metadata_path.read_bytes()).hexdigest(), metadata_arcname))
            archive.writestr("证据清单/SHA256SUMS.txt", "\n".join(hashes) + "\n")
        background_tasks.add_task(_remove_file, str(path))
        filename = "%s-%s-行政处罚证据包.zip" % (
            safe_filename(str(record["company_name"])),
            {"added": "新增", "deleted": "删除", "modified": "变更"}.get(str(record["change_type"]), "更新"),
        )
        return FileResponse(path, media_type="application/zip", filename=filename)

    @app.post("/api/v1/monitor/run", status_code=201)
    def run_monitor_now() -> Dict[str, Any]:
        configured = load_monitor_companies(company_file)
        if not configured:
            raise HTTPException(status_code=400, detail="固定企业名单为空，请先填写 monitor_companies.txt")
        active = next((task for task in store.list_tasks() if task["status"] in ("queued", "running", "paused", "pause_requested")), None)
        if active:
            raise HTTPException(status_code=409, detail="已有一轮采集正在执行，请等待完成后再手动更新")
        task = store.create_task("手动行政管理复查 · %s" % datetime.now().strftime("%Y-%m-%d %H:%M"), configured)
        manager.enqueue(str(task["id"]))
        return {"task": task}

    @app.post("/api/v1/monitor/credit-scores/run", status_code=201)
    def run_credit_scores_now() -> Dict[str, Any]:
        configured = load_monitor_companies(company_file)
        if not configured:
            raise HTTPException(status_code=400, detail="固定企业名单为空，请先添加企业")
        active = store.active_credit_score_job()
        if active is not None:
            raise HTTPException(status_code=409, detail="已有一轮信用分采集正在执行")
        job = store.create_credit_score_job(configured)
        credit_score_manager.enqueue(str(job["id"]))
        return {"task": job}

    @app.get("/api/v1/monitor/credit-scores")
    def monitor_credit_scores() -> Dict[str, Any]:
        return {
            "scores": store.list_credit_scores(),
            "task": store.latest_credit_score_job(),
        }

    @app.post("/api/v1/monitor/companies", status_code=201)
    def add_monitor_companies(payload: MonitorCompaniesRequest) -> Dict[str, Any]:
        additions = [name.strip() for name in payload.companies if name.strip()]
        if not additions:
            raise HTTPException(status_code=400, detail="请输入至少一家企业")
        with company_file_lock:
            existing = load_monitor_companies(company_file)
            updated = save_monitor_companies(company_file, [*existing, *additions])
        return {
            "companies": updated,
            "count": len(updated),
            "added": len(updated) - len(existing),
        }

    @app.delete("/api/v1/monitor/companies/{company_name}")
    def remove_monitor_company(company_name: str) -> Dict[str, Any]:
        name = company_name.strip()
        with company_file_lock:
            existing = load_monitor_companies(company_file)
            if name not in existing:
                raise HTTPException(status_code=404, detail="该企业不在固定名单中")
            updated = save_monitor_companies(company_file, [item for item in existing if item != name])
        return {"companies": updated, "count": len(updated), "removed": name}

    def _enqueue_single_company(company_name: str, label: str) -> Dict[str, Any]:
        name = company_name.strip()
        configured = load_monitor_companies(company_file)
        if configured and name not in configured:
            raise HTTPException(status_code=404, detail="该企业不在固定名单中，请先添加")
        active = next(
            (task for task in store.list_tasks() if task["status"] in ("queued", "running", "paused", "pause_requested")),
            None,
        )
        if active:
            raise HTTPException(status_code=409, detail="已有采集任务正在执行，请等待完成后再启动定向采集")
        task = store.create_task("%s · %s" % (label, datetime.now().strftime("%Y-%m-%d %H:%M")), [name])
        manager.enqueue(str(task["id"]))
        return {"task": task}

    @app.post("/api/v1/monitor/companies/{company_name}/run", status_code=201)
    def run_single_company(company_name: str) -> Dict[str, Any]:
        """定向采集：只对这一家企业复查行政许可 + 行政处罚并留存证据。"""

        return _enqueue_single_company(company_name, "定向行政管理复查")

    @app.post("/api/v1/monitor/companies/{company_name}/credit-score/run", status_code=201)
    def run_single_company_credit_score(company_name: str) -> Dict[str, Any]:
        name = company_name.strip()
        configured = load_monitor_companies(company_file)
        if configured and name not in configured:
            raise HTTPException(status_code=404, detail="该企业不在固定名单中，请先添加")
        active = store.active_credit_score_job()
        if active is not None:
            raise HTTPException(status_code=409, detail="已有一轮信用分采集正在执行")
        job = store.create_credit_score_job([name])
        credit_score_manager.enqueue(str(job["id"]))
        return {"task": job}

    @app.get("/api/v1/settings/env")
    def get_env_settings() -> Dict[str, Any]:
        """设置页读取当前凭据与采集参数。敏感键只返回掩码。"""

        file_values = _read_env_file(_env_file_path())
        fields = []
        for item in EDITABLE_ENV_KEYS:
            key = str(item["key"])
            raw = os.environ.get(key, file_values.get(key, ""))
            fields.append({
                "key": key,
                "label": item["label"],
                "hint": item["hint"],
                "sensitive": bool(item["sensitive"]),
                "value": (_ENV_MASK if raw else "") if item["sensitive"] else raw,
                "configured": bool(raw),
            })
        return {"fields": fields}

    @app.put("/api/v1/settings/env")
    def update_env_settings(payload: EnvSettingsRequest) -> Dict[str, Any]:
        """保存凭据到 .env.local 并同步进程内环境变量。

        敏感字段提交空字符串表示保持不变；提交 "__CLEAR__" 表示清空。
        """

        updates: Dict[str, str] = {}
        unknown = sorted(key for key in payload.values if key not in EDITABLE_ENV_KEY_SET)
        if unknown:
            raise HTTPException(status_code=400, detail="不支持修改的配置项：%s" % "、".join(unknown))
        for key, raw_value in payload.values.items():
            value = str(raw_value).strip()
            if key in SENSITIVE_ENV_KEYS:
                if not value or value == _ENV_MASK:
                    continue
                if value == "__CLEAR__":
                    value = ""
            updates[key] = value
        if updates:
            with env_file_lock:
                _update_env_file(_env_file_path(), updates)
            for key, value in updates.items():
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
        manager.dynamic_proxy_enabled = kuaidaili_enabled()
        try:
            manager.max_proxy_replacements = max(
                0, int(os.getenv("KDL_MAX_PROXY_REPLACEMENTS_PER_TASK", "20"))
            )
        except ValueError:
            pass
        try:
            manager.request_interval = max(
                0.0, float(os.getenv("CREDITCHINA_REQUEST_INTERVAL_SECONDS", "1"))
            )
        except ValueError:
            pass
        return {"saved": sorted(updates), "count": len(updates)}

    @app.get("/api/v1/settings/paths")
    def get_path_settings() -> Dict[str, Any]:
        fields = []
        for item in ENV_PATH_KEYS:
            key = str(item["key"])
            fields.append({
                "key": key,
                "label": item["label"],
                "hint": item["hint"],
                "value": _current_env_value(key),
            })
        return {
            "fields": fields,
            "active": {
                "outputDir": str(manager.output_dir),
                "statePath": str(store.path),
                "companyFile": str(company_file),
            },
        }

    @app.put("/api/v1/settings/paths")
    def update_path_settings(payload: PathSettingsRequest) -> Dict[str, Any]:
        """修改存储位置。路径写入 .env.local，重启服务后完全生效。"""

        unknown = sorted(key for key in payload.values if key not in ENV_PATH_KEY_SET)
        if unknown:
            raise HTTPException(status_code=400, detail="不支持修改的路径项：%s" % "、".join(unknown))
        updates: Dict[str, str] = {}
        for key, raw_value in payload.values.items():
            value = str(raw_value).strip()
            if value:
                updates[key] = value
        if not updates:
            raise HTTPException(status_code=400, detail="没有需要保存的路径修改")
        with env_file_lock:
            _update_env_file(_env_file_path(), updates)
        return {"saved": sorted(updates), "restartRequired": True}

    @app.get("/api/v1/exports/{mode}")
    def export_xlsx(
        mode: str,
        background_tasks: BackgroundTasks,
        company: str = Query(default=""),
    ) -> FileResponse:
        company_name = company.strip() or None
        handle, temporary = tempfile.mkstemp(prefix="creditchina-export-", suffix=".xlsx")
        os.close(handle)
        path = Path(temporary)
        try:
            if mode == "current":
                if not company_name:
                    raise HTTPException(status_code=400, detail="本次导出需要企业名称")
                record = store.latest_record(company_name)
                if record is None:
                    raise HTTPException(status_code=404, detail="该企业还没有成功采集记录")
                XlsxExporter.export_current_company(record, path)
                filename = "%s-本次真实采集.xlsx" % safe_filename(company_name)
            elif mode == "penalties":
                rows = store.history_rows("行政处罚", company_name)
                XlsxExporter.export_history_penalties(rows, path, company_name=company_name)
                filename = "%s.xlsx" % (safe_filename(company_name) + "-历史行政处罚" if company_name else "全部历史行政处罚")
            elif mode == "all":
                rows = store.history_rows(company_name=company_name)
                XlsxExporter.export_history_all(rows, path, company_name=company_name)
                filename = "%s.xlsx" % (safe_filename(company_name) + "-历史全部信息" if company_name else "全部历史信息")
            else:
                raise HTTPException(status_code=404, detail="不支持的导出类型")
        except Exception:
            _remove_file(str(path))
            raise
        background_tasks.add_task(_remove_file, str(path))
        return FileResponse(
            path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    app.state.task_store = store
    app.state.crawl_manager = manager
    return app


app = create_app()


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="信用中国本机真实采集 API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run(
        "creditchina_merged.web_api:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
