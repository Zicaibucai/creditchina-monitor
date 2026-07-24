"""SQLite 任务与历史仓（web API 用）。

服务复用 ``spider_main.py`` 使用的 BrowserClient 和
CreditChinaCrawler，并把任务、实际采集结果与只增不减的历史
保存在本机 SQLite 中。

代码按职责拆分为同包内的模块：

- ``api_store.py``     SQLite 任务与历史仓（TaskStore）
- ``api_jobs.py``      后台采集队列（CrawlManager / CreditScoreManager）
- ``api_settings.py``  环境变量与企业名单设置
- ``web_api.py``       FastAPI 应用与路由（本模块）
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

from fastapi import FastAPI, HTTPException, Query, Request
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

