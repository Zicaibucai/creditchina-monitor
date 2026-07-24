#!/usr/bin/env python3
"""Start the API and dashboard from any working directory."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    npm = shutil.which("npm")
    if not VENV_PYTHON.exists() or npm is None or not (ROOT / "frontend/node_modules").exists():
        raise SystemExit("依赖尚未安装，请先运行：python3 setup_project.py")

    backend = subprocess.Popen([str(VENV_PYTHON), str(ROOT / "api_server.py")], cwd=ROOT)
    frontend = subprocess.Popen([npm, "run", "dev"], cwd=ROOT / "frontend")
    print("系统启动中：http://localhost:3000 （按 Ctrl+C 停止）", flush=True)
    interrupted = False
    try:
        while backend.poll() is None and frontend.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop(frontend)
        stop(backend)

    if interrupted:
        return 0
    failed = [process.returncode for process in (backend, frontend) if process.returncode not in (None, 0, -15)]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
