"""本机真实采集 API。

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

from .api_jobs import CreditScoreManager, CrawlManager
from .api_settings import (
    DailyMonitorScheduler,
    EDITABLE_ENV_KEYS,
    EDITABLE_ENV_KEY_SET,
    ENV_PATH_KEYS,
    ENV_PATH_KEY_SET,
    SENSITIVE_ENV_KEYS,
    _ENV_MASK,
    _current_env_value,
    _env_file_path,
    _read_env_file,
    _remove_file,
    _update_env_file,
    load_monitor_companies,
    save_monitor_companies,
)
from .api_store import (
    DEFAULT_COMPANY_FILE,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_STATE_PATH,
    TaskStore,
    _canonical_json,
    _fingerprint,
    _penalty_identity,
    _project_path_from_env,
    _record_from_payload,
    _record_key,
    _utc_timestamp,
)

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

    api_token = os.getenv("CREDITCHINA_API_TOKEN", "").strip()

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

    if api_token:

        @app.middleware("http")
        async def require_api_token(request: Request, call_next):
            # 健康检查保持公开，便于本机脚本探活。
            # <img>/<a> 等标签无法携带请求头，因此同时接受 token 查询参数。
            if request.url.path != "/api/v1/health" and (
                request.headers.get("x-api-token", "") != api_token
                and request.query_params.get("token", "") != api_token
            ):
                from starlette.responses import JSONResponse

                return JSONResponse(status_code=401, content={"detail": "缺少或无效的 API 令牌"})
            return await call_next(request)

    cors_origins = [
        origin.strip()
        for origin in os.getenv("CREDITCHINA_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if not cors_origins:
        cors_origins = [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://[::1]:3000",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        # The settings page saves credentials and storage paths with PUT.
        # Browsers preflight cross-port requests, so omitting PUT here makes a
        # healthy local API look unreachable to the frontend.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Token"],
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
