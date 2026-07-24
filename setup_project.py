#!/usr/bin/env python3
"""Create an isolated environment and install backend/frontend dependencies."""

from __future__ import annotations

import shutil
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def run(command: list[str], cwd: Path = ROOT) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def venv_python() -> Path:
    relative = Path("Scripts/python.exe") if sys.platform == "win32" else Path("bin/python")
    return VENV / relative


def main() -> int:
    if sys.version_info < (3, 9):
        raise SystemExit("需要 Python 3.9 或更高版本。")
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise SystemExit("未找到 npm，请先安装 Node.js 22.13 或更高版本。")
    version_text = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
    try:
        node_version = tuple(int(part) for part in version_text.split(".")[:2])
    except ValueError as exc:
        raise SystemExit(f"无法识别 Node.js 版本：{version_text}") from exc
    if node_version < (22, 13):
        raise SystemExit(f"当前 Node.js 为 {version_text}，需要 22.13 或更高版本。")

    if not VENV.exists():
        print("正在创建 Python 虚拟环境……", flush=True)
        venv.EnvBuilder(with_pip=True).create(VENV)

    run([str(venv_python()), "-m", "pip", "install", "-e", "."])
    run([npm, "ci"], ROOT / "frontend")

    local_env = ROOT / ".env.local"
    if not local_env.exists():
        shutil.copyfile(ROOT / ".env.example", local_env)
        print("已生成 .env.local；使用代理或自动验证码时，请在其中填写凭据。")

    print("安装完成。运行 `python3 start_project.py` 启动系统。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
