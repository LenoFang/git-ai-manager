#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口（只用于 PyInstaller 构建）。

有参数时只加载命令行核心，没有参数时才加载图形界面 —— 提交钩子每次 commit 都会跑，
不应该为 tkinter 付出启动开销。

单独运行本文件同样是可用的：`python entry.py status` 等价于核心的 status。
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

# PyInstaller 只分析「入口脚本」的 import。核心与界面都是以数据文件形式打包的
# （文件名带连字符，无法直接 import），所以这里把它们用到的标准库都列出来，
# 保证冻结后不缺模块。tkinter 用 --hidden-import 引入，避免提交钩子为它付出启动开销。
import argparse  # noqa: F401
import contextlib  # noqa: F401
import datetime  # noqa: F401
import io  # noqa: F401
import json  # noqa: F401
import os
import queue  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import textwrap  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import unicodedata  # noqa: F401
import uuid  # noqa: F401
import ctypes  # noqa: F401

if os.name == "nt":  # pragma: no cover - 仅 Windows 存在
    import msvcrt  # noqa: F401


def _load(module_name: str, filename: str):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"找不到 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _emergency_log() -> None:
    """连核心都加载失败时，至少把栈写到 exe 旁边。"""
    try:
        base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
            else Path(__file__).resolve().parent
        target = base / "manager-logs"
        target.mkdir(parents=True, exist_ok=True)
        (target / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    argv = sys.argv[1:]
    try:
        if argv and argv[0] != "--selftest":
            manager = _load("git_ai_manager", "git-ai-manager.py")
            manager.ensure_streams()
            return manager.main(argv)
        gui = _load("git_ai_manager_gui", "git-ai-manager-gui.py")
        if argv and argv[0] == "--selftest":
            return gui.selftest()
        return gui.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except BaseException:
        _emergency_log()
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
