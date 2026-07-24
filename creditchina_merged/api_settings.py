"""看板设置：可编辑环境变量、固定企业名单与每日调度。"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .api_jobs import CrawlManager
from .api_store import TaskStore
from .config import PROJECT_ROOT

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
