#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-ai 管理器（图形界面）

一个基于 tkinter 的小工具，用来管理 git-ai 的提交归属数据：
仓库扫描 / 一键安装钩子 / 工具模型配置 / notes 同步 / 记录与日志。

界面语言为中文，Python 3.10+，只用标准库（tkinter 随官方 Python 安装包提供）。

    Windows:  python git-ai-manager-gui.py
    macOS  :  python3 git-ai-manager-gui.py

它在同一目录下调用 git-ai-manager.py（命令行核心），所有真正的逻辑都在那边，
界面只负责收集参数、展示结果。配置文件：

    gui-config.json    界面自身的引导设置（git-ai 目录、可执行文件、Python）
    manager.json       与命令行/hooks 共用的配置（源码目录、深度、tool/model）

没有任何路径是写死的：git-ai 目录默认取本脚本所在目录，其余在运行时探测，
也可以在「设置」页里改。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import queue
import sys
import threading
import traceback
from pathlib import Path

# git-ai-manager.py 是以「数据文件」形式打进包的（文件名带连字符，PyInstaller 无法
# 把它当作模块做静态分析），所以要在这里显式导入它用到的标准库，保证冻结后不缺模块。
import argparse  # noqa: F401
import datetime  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import textwrap  # noqa: F401
import time  # noqa: F401
import unicodedata  # noqa: F401
import uuid  # noqa: F401
import ctypes  # noqa: F401
if os.name == "nt":  # pragma: no cover - 仅 Windows 存在
    import msvcrt  # noqa: F401
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # pragma: no cover - depends on the interpreter build
    sys.stderr.write(
        "当前 Python 没有 tkinter，无法启动界面。\n"
        "  Windows/macOS 请使用 python.org 的官方安装包（自带 tkinter）。\n"
        "  macOS(Homebrew): brew install python-tk\n"
        "  Linux: sudo apt install python3-tk\n"
        "只使用命令行时可以直接运行 git-ai-manager.py。\n"
    )
    raise SystemExit(2)

APP_TITLE = "git-ai 管理器"
MANAGER_FILENAME = "git-ai-manager.py"
GUI_CONFIG_NAME = "gui-config.json"
CONSOLE_LIMIT = 4000
PAD = 6
RECORDS_ALL_LABEL = "（全部仓库 - 较慢）"
BACKFILL_LIMIT = 200


def script_dir() -> Path:
    """Folder holding this program's files (a temp unpack dir when frozen)."""
    return Path(__file__).resolve().parent


def app_dir() -> Path:
    """Folder the user sees: next to the exe when frozen, else the source dir.

    开发布局（exe 在 <工具目录>/dist/ 下、同目录还有源码）时用工具目录，
    这样 exe 和源码共用一份配置/日志；macOS 的 .app 内部不能写用户数据，
    所以取 .app 所在目录。
    """
    if getattr(sys, "frozen", False):
        try:
            exe = Path(sys.executable).resolve()
        except OSError:
            return script_dir()
        for parent in list(exe.parents)[:3]:
            if (parent / MANAGER_FILENAME).is_file():
                return parent
        parts = exe.parts
        lowered = [part.lower() for part in parts]
        if "contents" in lowered and "macos" in lowered:
            index = lowered.index("contents")
            if index > 0 and parts[index - 1].endswith(".app"):
                return Path(*parts[: index - 1])
        return exe.parent
    return script_dir()


def load_manager_module(path: Path):
    """把 git-ai-manager.py 作为模块导入（文件名带连字符，只能走 importlib）。"""
    spec = importlib.util.spec_from_file_location("git_ai_manager", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["git_ai_manager"] = module
    spec.loader.exec_module(module)
    return module


class GuiSettings:
    """界面自身的引导设置；业务配置一律写进 manager.json。"""

    DEFAULTS = {
        "gitAiHome": "",        # 空 = 本脚本所在目录
        "gitAiExe": "",         # 空 = 自动探测
        "configPath": "",       # 空 = <home>/manager.json
        "python": "",           # 空 = 自动探测
        "depth": 4,
        "lastTab": 0,
        "geometry": "",
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = dict(self.DEFAULTS)
        try:
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    self.data.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        try:
            payload = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
            self.path.write_text(payload, encoding="utf-8", newline="\n")
        except OSError:
            pass


class StreamRouter:
    """Thread aware stdout/stderr router.

    contextlib.redirect_stdout replaces the process wide stream, so anything
    another thread prints while a task is running would land inside that task's
    capture buffer (and, for --json output, break the parse).  This router keeps
    a per thread buffer: the worker that asked for capture gets everything it
    prints, every other thread keeps writing to the real stream.
    """

    def __init__(self, real_out, real_err) -> None:
        self.real_out = real_out
        self.real_err = real_err
        self.local = threading.local()

    @contextlib.contextmanager
    def capture(self):
        buffer = io.StringIO()
        previous = getattr(self.local, "buffer", None)
        self.local.buffer = buffer
        try:
            yield buffer
        finally:
            self.local.buffer = previous

    def _target(self, real):
        buffer = getattr(self.local, "buffer", None)
        return buffer if buffer is not None else real


class _RoutedStream(io.TextIOBase):
    def __init__(self, router: StreamRouter, real) -> None:
        self._router = router
        self._real = real

    def write(self, text: str):
        return self._router._target(self._real).write(text)

    def flush(self) -> None:
        target = self._router._target(self._real)
        try:
            target.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:
        return False

    def fileno(self):
        return self._real.fileno()

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")


def install_stream_router() -> StreamRouter:
    router = StreamRouter(sys.stdout, sys.stderr)
    sys.stdout = _RoutedStream(router, router.real_out)
    sys.stderr = _RoutedStream(router, router.real_err)
    return router


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.withdraw()
        self.title(APP_TITLE)
        self.minsize(900, 620)

        self.settings = GuiSettings(app_dir() / GUI_CONFIG_NAME)
        self.manager_path = script_dir() / MANAGER_FILENAME
        if not self.manager_path.is_file():
            messagebox.showerror(
                APP_TITLE,
                f"找不到命令行核心：\n{self.manager_path}\n\n"
                f"请把 {MANAGER_FILENAME} 和本程序放在同一目录。",
            )
            raise SystemExit(2)
        self.manager = load_manager_module(self.manager_path)
        self.manager.ensure_streams()  # windowed exe: recover stdout/stderr first
        self.router = install_stream_router()

        self.config: dict = {}
        self.last_raw_output = ""
        self.repo_rows: list[dict] = []
        self.trees: dict[str, ttk.Treeview] = {}
        self.busy = False
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.console_lines = 0
        self.action_buttons: list[ttk.Button] = []

        self._apply_env()
        self._build_style()
        self._build_layout()
        self.load_config_into_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(80, self._pump)

        geometry = self.settings.data.get("geometry")
        if geometry:
            try:
                self.geometry(geometry)
            except tk.TclError:
                self.geometry("1040x740")
        else:
            self.geometry(self._centered_geometry(1040, 740))
        self.deiconify()
        self.after(200, self.refresh_environment)

    # ------------------------------------------------------------ 基础
    def _centered_geometry(self, width: int, height: int) -> str:
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        return f"{width}x{height}+{x}+{y}"

    def _apply_env(self) -> None:
        """让核心模块按界面里的设置解析路径。"""
        home = self.settings.data.get("gitAiHome") or str(script_dir())
        os.environ["GIT_AI_HOME"] = str(Path(home).expanduser())
        exe = self.settings.data.get("gitAiExe")
        if exe:
            os.environ["GIT_AI_EXE"] = exe
        else:
            os.environ.pop("GIT_AI_EXE", None)
        python = self.settings.data.get("python")
        if python:
            os.environ["GIT_AI_PYTHON"] = python
        else:
            os.environ.pop("GIT_AI_PYTHON", None)

    @property
    def home(self) -> str:
        return str(Path(self.settings.data.get("gitAiHome") or app_dir()).expanduser())

    def _build_style(self) -> None:
        style = ttk.Style(self)
        for candidate in ("vista", "aqua", "clam", "default"):
            if candidate in style.theme_names():
                try:
                    style.theme_use(candidate)
                    break
                except tk.TclError:
                    continue
        self.option_add("*Treeview*Font", "TkDefaultFont")

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=0)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=PAD, pady=(PAD, 2))

        self.tab_settings = ttk.Frame(self.notebook, padding=PAD)
        self.tab_repos = ttk.Frame(self.notebook, padding=PAD)
        self.tab_sync = ttk.Frame(self.notebook, padding=PAD)
        self.tab_records = ttk.Frame(self.notebook, padding=PAD)
        self.tab_logs = ttk.Frame(self.notebook, padding=PAD)
        for frame, title in (
            (self.tab_settings, "设置"),
            (self.tab_repos, "仓库"),
            (self.tab_sync, "同步"),
            (self.tab_records, "记录"),
            (self.tab_logs, "日志"),
        ):
            self.notebook.add(frame, text=title)

        self._build_settings_tab()
        self._build_repos_tab()
        self._build_sync_tab()
        self._build_records_tab()
        self._build_logs_tab()

        self.action_buttons: list[ttk.Button] = []
        self._collect_action_buttons(self)

        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, sticky="ew", padx=PAD, pady=(0, PAD))
        bottom.columnconfigure(0, weight=1)

        console_frame = ttk.LabelFrame(bottom, text="运行输出", padding=4)
        console_frame.grid(row=0, column=0, sticky="ew")
        console_frame.columnconfigure(0, weight=1)
        self.console = tk.Text(console_frame, height=9, wrap="none", font="TkFixedFont",
                               background="#1f1f1f", foreground="#e6e6e6",
                               insertbackground="#e6e6e6", borderwidth=0)
        self.console.grid(row=0, column=0, sticky="ew")
        console_vsb = ttk.Scrollbar(console_frame, orient="vertical", command=self.console.yview)
        console_vsb.grid(row=0, column=1, sticky="ns")
        console_hsb = ttk.Scrollbar(console_frame, orient="horizontal", command=self.console.xview)
        console_hsb.grid(row=1, column=0, sticky="ew")
        self.console.configure(yscrollcommand=console_vsb.set, xscrollcommand=console_hsb.set,
                               state="disabled")
        self.console.tag_configure("err", foreground="#ff8080")
        self.console.tag_configure("ok", foreground="#8ce99a")

        bar = ttk.Frame(bottom)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        bar.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.progress.grid(row=0, column=1, padx=6)

    def _make_table(self, parent, columns, height=12):
        """columns: [(key, title, width, anchor, stretch)]"""
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        keys = [c[0] for c in columns]
        tree = ttk.Treeview(frame, columns=keys, show="headings", height=height,
                            selectmode="extended")
        for key, title, width, anchor, stretch in columns:
            tree.heading(key, text=title, command=lambda k=key, t=tree: self._sort_table(t, k))
            tree.column(key, width=width, anchor=anchor, stretch=stretch)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        return frame, tree

    def _sort_table(self, tree: ttk.Treeview, key: str) -> None:
        rows = [(tree.set(item, key), item) for item in tree.get_children("")]
        reverse = getattr(tree, "_sort_desc", {}).get(key, False) if hasattr(tree, "_sort_desc") else False

        def sort_key(pair):
            text = pair[0]
            try:
                return (0, float(text))
            except (TypeError, ValueError):
                return (1, str(text).lower())

        rows.sort(key=sort_key, reverse=reverse)
        for position, (_value, item) in enumerate(rows):
            tree.move(item, "", position)
        if not hasattr(tree, "_sort_desc"):
            tree._sort_desc = {}
        tree._sort_desc[key] = not reverse

    # ------------------------------------------------------------ 任务调度
    def call(self, argv: list[str]) -> tuple[int, str]:
        """在进程内执行一条核心命令，捕获输出（线程安全）。"""
        code = 0
        base = ["--home", self.home]
        config_path = (self.settings.data.get("configPath") or "").strip()
        if config_path:
            base += ["--config", config_path]
        with self.router.capture() as buffer:
            try:
                code = self.manager.main(base + argv)
            except SystemExit as exc:
                payload = exc.code
                code = payload if isinstance(payload, int) else 1
                if isinstance(payload, str):
                    print(payload)
            except Exception:
                print(traceback.format_exc())
                code = 1
        return code, buffer.getvalue()

    def call_json(self, argv: list[str]):
        code, text = self.call(argv)
        self.last_raw_output = text
        stripped = text.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None

    def submit(self, label: str, work, done=None) -> None:
        """在后台线程执行 work()，完成后回调 done(ok, payload)。"""
        if self.busy:
            self.log(f"上一个任务还没结束，已忽略：{label}", "err")
            self.status_var.set("忙碌中 - 请稍候再试")
            return
        self.busy = True
        self._set_buttons_enabled(False)
        self.status_var.set(label + " ...")
        self.progress.start(12)

        def runner():
            try:
                payload = work()
                self.queue.put(("task", label, True, payload, done))
            except Exception:
                self.queue.put(("task", label, False, traceback.format_exc(), done))

        threading.Thread(target=runner, daemon=True).start()

    def _collect_action_buttons(self, widget) -> None:
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                self.action_buttons.append(child)
            self._collect_action_buttons(child)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.action_buttons:
            try:
                button.configure(state=state)
            except tk.TclError:
                pass

    def _pump(self) -> None:
        try:
            while True:
                kind, label, ok, payload, done = self.queue.get_nowait()
                if kind == "task":
                    self.busy = False
                    self._set_buttons_enabled(True)
                    self.progress.stop()
                    self.status_var.set(label + (" 完成" if ok else " 失败"))
                    if done is not None:
                        done(ok, payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def log(self, text: str, tag: str = "") -> None:
        if not text:
            return
        self.console.configure(state="normal")
        if not text.endswith("\n"):
            text += "\n"
        self.console.insert("end", text, tag)
        self.console_lines += text.count("\n")
        if self.console_lines > CONSOLE_LIMIT:
            self.console.delete("1.0", f"{self.console_lines - CONSOLE_LIMIT}.0")
            self.console_lines = CONSOLE_LIMIT
        self.console.see("end")
        self.console.configure(state="disabled")

    def task_call(self, label: str, argv: list[str], done=None):
        """执行一条命令，把输出打到控制台。"""
        def work():
            return self.call(argv)
        def finish(ok, payload):
            code, text = payload
            self.log(f"$ {self.manager.APP} {' '.join(argv)}")
            self.log(text.rstrip())
            if code != 0:
                self.log(f"(退出码 {code})", "err")
            if done is not None:
                done(code == 0, payload)
        self.submit(label, work, finish)

    # ------------------------------------------------------------ 设置页
    def _build_settings_tab(self) -> None:
        frame = self.tab_settings
        frame.columnconfigure(1, weight=1)
        row = 0

        def label(text, r):
            ttk.Label(frame, text=text).grid(row=r, column=0, sticky="w", pady=3)

        label("git-ai 目录", row)
        self.var_home = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.var_home)
        entry.grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(frame, text="浏览…", width=8,
                   command=self.pick_home).grid(row=row, column=2, padx=2)
        row += 1
        ttk.Label(frame, text="存放 manager.json / notes-repo-map / 本程序的位置",
                  foreground="#666").grid(row=row, column=1, sticky="w", padx=4)
        row += 1

        label("git-ai 可执行文件", row)
        self.var_exe = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_exe).grid(row=row, column=1, sticky="ew", padx=4)
        exe_buttons = ttk.Frame(frame)
        exe_buttons.grid(row=row, column=2, sticky="e", padx=2)
        ttk.Button(exe_buttons, text="自动检测", width=9,
                   command=self.detect_git_ai).grid(row=0, column=0, padx=2)
        ttk.Button(exe_buttons, text="浏览…", width=8,
                   command=self.pick_exe).grid(row=0, column=1, padx=2)
        row += 1
        self.exe_hint = ttk.Label(
            frame,
            text="留空 = 自动探测：PATH → 工具目录/bin → ~/.git-ai/bin → 其它常见位置",
            foreground="#666",
        )
        self.exe_hint.grid(row=row, column=1, columnspan=2, sticky="w", padx=4)
        row += 1

        label("hook 使用的 Python", row)
        self.var_python = tk.StringVar()
        if getattr(sys, "frozen", False):
            # 独立 exe：钩子直接调用本程序，不存在“用哪个 Python”的问题
            hint_text, hint_colour, hint_span = (
                "不需要 —— 独立 exe 运行时钩子直接调用本程序，无需 Python", "#2b8a3e", 2)
        else:
            ttk.Entry(frame, textvariable=self.var_python).grid(row=row, column=1, sticky="ew", padx=4)
            py_buttons = ttk.Frame(frame)
            py_buttons.grid(row=row, column=2, sticky="e", padx=2)
            ttk.Button(py_buttons, text="浏览…", width=8,
                       command=self.pick_python).grid(row=0, column=0, padx=2)
            ttk.Button(py_buttons, text="建 .venv", width=9,
                       command=self.create_venv).grid(row=0, column=1, padx=2)
            hint_text, hint_colour, hint_span = (
                "留空 = 自动探测（优先用本目录下的 .venv，其次是本机 Python）", "#666", 1)
        self.python_hint = ttk.Label(frame, text=hint_text, foreground=hint_colour)
        self.python_hint.grid(row=row + 1, column=1, columnspan=hint_span, sticky="w", padx=4)
        row += 2

        label("默认工具 / 模型", row)
        tool_model = ttk.Frame(frame)
        tool_model.grid(row=row, column=1, columnspan=2, sticky="ew", padx=4)
        tool_model.columnconfigure(1, weight=1)
        tool_model.columnconfigure(3, weight=1)
        ttk.Label(tool_model, text="工具").grid(row=0, column=0, padx=(0, 4))
        self.var_tool = tk.StringVar(value="codex")
        ttk.Entry(tool_model, textvariable=self.var_tool).grid(row=0, column=1, sticky="ew")
        ttk.Label(tool_model, text="模型").grid(row=0, column=2, padx=(12, 4))
        self.var_model = tk.StringVar(value="unknown")
        ttk.Entry(tool_model, textvariable=self.var_model).grid(row=0, column=3, sticky="ew")
        row += 1

        label("扫描深度", row)
        depth_frame = ttk.Frame(frame)
        depth_frame.grid(row=row, column=1, columnspan=2, sticky="ew", padx=4)
        self.var_depth = tk.IntVar(value=4)
        ttk.Spinbox(depth_frame, from_=1, to=12, width=5,
                    textvariable=self.var_depth).grid(row=0, column=0)
        ttk.Label(depth_frame, text="层：在源码目录下最多往下找几层仓库（1 = 只看第一层）"
                  ).grid(row=0, column=1, padx=8, sticky="w")
        row += 1

        label("源码目录", row)
        roots_frame = ttk.Frame(frame)
        roots_frame.grid(row=row, column=1, columnspan=2, sticky="nsew", padx=4)
        roots_frame.columnconfigure(0, weight=1)
        roots_frame.rowconfigure(0, weight=1)
        frame.rowconfigure(row, weight=1)
        self.roots_list = tk.Listbox(roots_frame, height=6, activestyle="none")
        self.roots_list.grid(row=0, column=0, sticky="nsew")
        roots_vsb = ttk.Scrollbar(roots_frame, orient="vertical", command=self.roots_list.yview)
        roots_vsb.grid(row=0, column=1, sticky="ns")
        self.roots_list.configure(yscrollcommand=roots_vsb.set)
        buttons = ttk.Frame(roots_frame)
        buttons.grid(row=0, column=2, sticky="n", padx=(6, 0))
        ttk.Button(buttons, text="添加…", width=10, command=self.add_root).grid(row=0, column=0, pady=2)
        ttk.Button(buttons, text="删除", width=10, command=self.remove_root).grid(row=1, column=0, pady=2)
        row += 1
        ttk.Label(frame, text="默认 4 层；扫描时会自动跳过 node_modules / target / .venv 等目录",
                  foreground="#666").grid(row=row, column=1, sticky="w", padx=4)
        row += 1

        actions = ttk.Frame(frame)
        actions.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        ttk.Button(actions, text="保存设置", command=self.save_settings).grid(row=0, column=0, padx=2)
        ttk.Button(actions, text="检测环境", command=self.refresh_environment).grid(row=0, column=1, padx=2)
        ttk.Button(actions, text="打开配置目录", command=self.open_home).grid(row=0, column=2, padx=2)
        row += 1

        self.env_text = tk.Text(frame, height=8, wrap="word", font="TkFixedFont")
        self.env_text.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        frame.rowconfigure(row, weight=1)
        self.env_text.configure(state="disabled")

    def pick_home(self) -> None:
        chosen = filedialog.askdirectory(title="选择 git-ai 目录", initialdir=self.home)
        if chosen:
            self.var_home.set(chosen)

    def detect_git_ai(self) -> None:
        """自动检测 git-ai：找到就填进去，找不到就列出找过哪些位置。"""
        pinned = self.var_exe.get().strip()
        # 界面会把设置值镜像到 GIT_AI_EXE，先摘掉它，才能看出“自动探测”真正会选哪一个
        env_name = self.manager.ENV_GIT_AI_EXE
        mirrored = os.environ.pop(env_name, None)
        try:
            path, origin = self.manager.find_git_ai_with_origin()
        finally:
            if mirrored is not None:
                os.environ[env_name] = mirrored
        lines: list[str] = []
        if path:
            version = self.manager.git_ai_version()
            self.var_exe.set(path)
            lines.append(f"已找到 git-ai：{path}")
            lines.append(f"  自动探测的来源：{origin}")
            lines.append(f"  版本：{version}")
            lines.append(f"  之前设置的是：{pinned or '（留空 —— 每次自动探测）'}")
            lines.append("")
            lines.append("已填入上面的输入框（留空则每次自动探测）。")
        else:
            lines.append("没有找到 git-ai。以下位置都找过了：")
            for candidate, why in self.manager.git_ai_search_paths():
                lines.append(f"  - {candidate}\n      [{why}]")
            lines.append("")
            lines.append("解决办法（任选其一）：")
            lines.append("  1. 安装 git-ai，然后重新打开本程序；")
            lines.append("  2. 把 git-ai.exe 放到「工具目录\\bin\\」下（便携包做法）；")
            lines.append("  3. 点「浏览…」手动指定 git-ai 可执行文件；")
            lines.append("  4. 设置环境变量 GIT_AI_EXE 指向它。")
        text = "\n".join(lines)
        self.log(text)
        self._show_text("git-ai 检测结果", text)

    def pick_exe(self) -> None:
        patterns = [("git-ai", "git-ai*"), ("所有文件", "*")]
        chosen = filedialog.askopenfilename(title="选择 git-ai 可执行文件", filetypes=patterns)
        if chosen:
            self.var_exe.set(chosen)

    def pick_python(self) -> None:
        patterns = [("Python", "python*"), ("所有文件", "*")]
        chosen = filedialog.askopenfilename(title="选择 Python 解释器", filetypes=patterns)
        if chosen:
            self.var_python.set(chosen)

    def create_venv(self) -> None:
        """在工具目录下建一个虚拟环境，让 hook 的 Python 固定在本目录内。"""
        target = Path(self.home) / ".venv"
        if target.exists():
            self._use_local_python("虚拟环境已存在，直接使用")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"在工具目录下创建虚拟环境？\n\n{target}\n\n"
            "好处：hook 使用的 Python 固定在本目录内，不会因为 PATH 顺序或系统 "
            "Python 版本变化而失效，也不用 pip 装任何东西（本工具只用标准库）。\n\n"
            "注意：虚拟环境仍然依赖创建它的那个 Python 安装，如果那个安装被卸载，"
            "本工具会自动回退到其他解释器。",
        ):
            return
        base_python = self.manager.find_python(self.settings.data.get("python") or None)
        self.log(f"使用 {base_python} 创建虚拟环境 …")

        def work():
            return self.manager.run([base_python, "-m", "venv", str(target)], timeout=900)

        def finish(ok, payload):
            if not ok or payload is None or payload.returncode != 0:
                detail = "" if not ok or payload is None else (payload.stderr or payload.stdout or "")
                self.log(f"创建虚拟环境失败：{detail}".rstrip(), "err")
                return
            self._use_local_python("虚拟环境创建完成")

        self.submit("创建虚拟环境", work, finish)

    def _use_local_python(self, why: str) -> None:
        resolved = self.manager.local_python()
        if not resolved:
            self.log("没有在工具目录下找到可用的 Python（.venv / python / runtime）", "err")
            return
        # 留空即可让核心自动优先使用本目录的解释器，换机器也能用
        self.var_python.set("")
        self.settings.data["python"] = ""
        self.settings.save()
        self.python_hint.configure(text=f"hook 将使用：{resolved}")
        self.log(f"{why}，hook 的 Python = {resolved}")
        self.refresh_environment()

    def add_root(self) -> None:
        chosen = filedialog.askdirectory(title="选择源码目录")
        if not chosen:
            return
        existing = [self.roots_list.get(i) for i in range(self.roots_list.size())]
        if any(os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(chosen))
               for p in existing):
            return
        self.roots_list.insert("end", os.path.abspath(chosen))

    def remove_root(self) -> None:
        for index in reversed(self.roots_list.curselection()):
            self.roots_list.delete(index)

    def open_home(self) -> None:
        path = Path(self.home)
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", str(path)])
            elif os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showwarning(APP_TITLE, f"无法打开目录：{exc}")

    def load_config_into_ui(self) -> None:
        self._apply_env()
        config = self.manager.load_config()
        if not isinstance(config, dict):
            config = self.manager.default_config()
        self.config = config
        self.var_home.set(self.settings.data.get("gitAiHome") or str(app_dir()))
        self.var_exe.set(self.settings.data.get("gitAiExe") or "")
        self.var_python.set(self.settings.data.get("python") or "")
        defaults = config.get("defaults") or {}
        self.var_tool.set(str(defaults.get("tool") or "codex"))
        self.var_model.set(str(defaults.get("model") or "unknown"))
        try:
            depth = int(defaults.get("repoDepth", 4))
        except (TypeError, ValueError):
            depth = 4
        self.var_depth.set(max(1, min(depth, 12)))
        self.roots_list.delete(0, "end")
        for root in config.get("projectsRoots") or []:
            self.roots_list.insert("end", str(root))

    def save_settings(self, silent: bool = False) -> None:
        self.settings.data["gitAiHome"] = self.var_home.get().strip()
        self.settings.data["gitAiExe"] = self.var_exe.get().strip()
        self.settings.data["python"] = self.var_python.get().strip()
        self.settings.data["depth"] = int(self.var_depth.get() or 4)
        self.settings.save()
        self._apply_env()
        # 重新读盘，避免用界面里的旧快照覆盖 repos 等其他配置
        config = self.manager.load_config()
        if not isinstance(config, dict):
            config = self.manager.default_config()
        defaults = config.setdefault("defaults", {})
        defaults["tool"] = self.var_tool.get().strip() or "codex"
        defaults["model"] = self.var_model.get().strip() or "unknown"
        defaults["repoDepth"] = int(self.var_depth.get() or 4)
        roots = [self.roots_list.get(i) for i in range(self.roots_list.size())]
        config["projectsRoots"] = roots
        try:
            self.manager.save_config(config)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"写入 manager.json 失败：{exc}")
            return
        self.config = config
        if not silent:
            self.log(f"已保存设置：深度 {defaults['repoDepth']}，源码目录 {len(roots)} 个，"
                     f"默认 {defaults['tool']}::{defaults['model']}")
            self.status_var.set("设置已保存")

    def refresh_environment(self) -> None:
        # 注意：Tk 变量只能在主线程读取，先把值取出来再交给后台线程
        tk_version = str(self.tk.call("info", "patchlevel"))
        depth = int(self.var_depth.get() or 4)

        def work():
            frozen = bool(getattr(sys, "frozen", False))
            data = {
                "home": self.home,
                "config": str(self.manager.config_file()),
                "manager": str(self.manager_path),
                "运行方式": "独立 exe（目标机器不需要 Python）" if frozen else "Python 源码",
                "python": ("（内置 exe，无需 Python）" if frozen
                           else self.manager.find_python(self.settings.data.get("python") or None)),
                "git-ai": self.manager.find_git_ai()
                or "(未找到：请安装 git-ai 或在设置里指定可执行文件)",
                "git-ai 版本": self.manager.git_ai_version(),
                "git": self.manager.git_line(["--version"]) or "(未找到)",
                "platform": f"{sys.platform} / Python {sys.version.split()[0]}",
                "tk": tk_version,
            }
            executable, origin = self.manager.find_git_ai_with_origin()
            exe = executable
            if exe:
                data["git-ai 来源"] = origin
                probe = self.manager.run([exe, "config", "git_ai_hooks.post_notes_updated"])
                text = (probe.stdout or "").strip().replace("\n", " ")
                data["git-ai hook"] = text[:160] if text else "(未配置)"
                flags = [k for k in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "AGENT_OS",
                                     "GIT_AI_SKIP_ALL_HOOKS", "GIT_AI_SKIP_MANAGED_HOOKS")
                         if os.environ.get(k)]
                data["跳过 hook 的环境变量"] = ", ".join(flags) if flags else "(无)"
            try:
                data["仓库数量(当前深度)"] = len(self.manager.find_repos(
                    [self.manager.expand(r) for r in (self.config.get("projectsRoots") or [])],
                    depth=depth))
            except Exception as exc:
                data["仓库数量(当前深度)"] = f"扫描失败：{exc}"
            return data

        def finish(ok, payload):
            self.env_text.configure(state="normal")
            self.env_text.delete("1.0", "end")
            if not ok:
                self.env_text.insert("end", str(payload))
            else:
                width = max(len(k) for k in payload)
                for key, value in payload.items():
                    self.env_text.insert("end", f"{key.ljust(width)} : {value}\n")
                if (not getattr(sys, "frozen", False) and not self.var_python.get().strip()
                        and payload.get("python")):
                    self.python_hint.configure(text=f"hook 将使用：{payload['python']}")
            self.env_text.configure(state="disabled")

        self.submit("检测环境", work, finish)

    # ------------------------------------------------------------ 仓库页
    def _build_repos_tab(self) -> None:
        frame = self.tab_repos
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.btn_scan = ttk.Button(toolbar, text="扫描仓库", command=self.scan_repos)
        self.btn_scan.grid(row=0, column=0, padx=2)
        ttk.Button(toolbar, text="一键安装钩子", command=self.install_all).grid(row=0, column=1, padx=2)
        ttk.Button(toolbar, text="安装选中", command=self.install_selected).grid(row=0, column=2, padx=2)
        ttk.Button(toolbar, text="卸载", command=self.uninstall_selected).grid(row=0, column=3, padx=2)
        ttk.Button(toolbar, text="归一化选中", command=self.normalize_selected).grid(row=0, column=4, padx=2)
        ttk.Button(toolbar, text="修复空 note", command=self.repair_selected).grid(row=0, column=5, padx=2)
        ttk.Button(toolbar, text="补全历史", command=self.backfill_selected).grid(row=0, column=6, padx=2)
        ttk.Button(toolbar, text="诊断", command=self.diagnose_selected).grid(row=0, column=7, padx=2)
        ttk.Button(toolbar, text="设置工具模型", command=self.edit_repo_tool).grid(row=0, column=8, padx=2)

        columns = [
            ("name", "仓库", 150, "w", False),
            ("branch", "分支", 150, "w", False),
            ("head", "HEAD", 80, "w", False),
            ("notes", "笔记", 60, "e", False),
            ("mock", "mock_ai", 70, "e", False),
            ("human", "human", 60, "e", False),
            ("remote", "远端", 130, "w", False),
            ("tool", "工具::模型", 150, "w", False),
            ("hooks", "钩子", 70, "center", False),
            ("path", "路径", 380, "w", True),
        ]
        holder, tree = self._make_table(frame, columns, height=14)
        holder.grid(row=1, column=0, sticky="nsew")
        self.trees["repos"] = tree
        tree.bind("<Double-1>", lambda _e: self.edit_repo_tool())
        tree.tag_configure("pending", background="#fff4e6")
        tree.tag_configure("legacy", background="#f1f3f5")
        tree.tag_configure("mgr", background="#e7f5ff")

        hint = ttk.Label(
            frame,
            text="双击一行可单独设置该仓库的工具/模型；钩子列 mgr=本工具管理，ps1=旧 PowerShell，other=第三方",
            foreground="#666",
        )
        hint.grid(row=2, column=0, sticky="w", pady=(4, 0))

    def scan_repos(self) -> None:
        argv = ["status", "--json"]
        for index in range(self.roots_list.size()):
            argv += ["--root", self.roots_list.get(index)]
        argv += ["--depth", str(int(self.var_depth.get() or 4))]

        def work():
            return self.call_json(argv)

        def finish(ok, payload):
            if not ok or payload is None:
                self.log("扫描失败，请检查源码目录设置：", "err")
                if self.last_raw_output.strip():
                    self.log(self.last_raw_output.rstrip())
                return
            self.repo_rows = payload
            tree = self.trees["repos"]
            tree.delete(*tree.get_children(""))
            for row in payload:
                tags = []
                if row.get("mock_ai"):
                    tags.append("pending")
                hook = str(row.get("hooks", ""))
                if hook.startswith("mgr"):
                    tags.append("mgr")
                elif hook.startswith("ps1"):
                    tags.append("legacy")
                tree.insert(
                    "", "end", iid=row.get("repo"),
                    values=(
                        row.get("name") or Path(row.get("repo", "")).name,
                        row.get("branch", ""),
                        row.get("head", "") + ("*" if row.get("dirty") else ""),
                        row.get("notes", 0),
                        row.get("mock_ai", 0),
                        row.get("human", 0),
                        row.get("remote", ""),
                        f"{row.get('tool','')}::{row.get('model','')}",
                        hook,
                        row.get("repo", ""),
                    ),
                    tags=tags,
                )
            pending = sum(1 for r in payload if r.get("mock_ai"))
            self.status_var.set(f"扫描到 {len(payload)} 个仓库，其中 {pending} 个有未归一化的 mock_ai note")
            self.log(f"扫描完成：{len(payload)} 个仓库（深度 {self.var_depth.get()}）")
            self.refresh_sync_choices()

        self.submit(f"扫描仓库（深度 {self.var_depth.get()}）", work, finish)

    def selected_repos(self) -> list[str]:
        tree = self.trees["repos"]
        return list(tree.selection())

    def require_selection(self) -> list[str]:
        selection = self.selected_repos()
        if not selection:
            messagebox.showinfo(APP_TITLE, "请先在表格里选择至少一个仓库。")
        return selection

    def install_all(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "将为所有源码目录下的仓库安装 pre-commit / post-commit 钩子，"
            "并把 git-ai 的全局 post_notes_updated 指向本工具。\n\n继续？",
        ):
            return
        argv = ["install"]
        for index in range(self.roots_list.size()):
            argv += ["--root", self.roots_list.get(index)]
        argv += ["--depth", str(int(self.var_depth.get() or 4))]
        if self.var_python.get().strip():
            argv += ["--python", self.var_python.get().strip()]
        self.task_call("一键安装钩子", argv, lambda ok, _p: self.scan_repos() if ok else None)

    def install_selected(self) -> None:
        selection = self.require_selection()
        if not selection:
            return
        if not messagebox.askyesno(APP_TITLE, f"为选中的 {len(selection)} 个仓库安装钩子？"):
            return

        def work():
            lines = []
            for repo in selection:
                code, text = self.call(["install", "--repo", repo, "--no-git-ai-config"])
                lines.append(f"--- {repo} (exit {code}) ---\n{text.strip()}")
            return "\n".join(lines)

        def finish(_ok, payload):
            self.log(payload)
            self.log("提示：全局 git-ai hook 未被改动；需要时用「一键安装钩子」。")
            self.scan_repos()

        self.submit(f"安装 {len(selection)} 个仓库", work, finish)

    def uninstall_selected(self) -> None:
        selection = self.require_selection()
        if not selection:
            return
        if not messagebox.askyesno(
            APP_TITLE, f"卸载选中的 {len(selection)} 个仓库里的本工具钩子？\n（第三方钩子内容会保留）"
        ):
            return

        def work():
            lines = []
            for repo in selection:
                code, text = self.call(["uninstall", "--repo", repo, "--keep-git-ai-config"])
                lines.append(f"--- {repo} (exit {code}) ---\n{text.strip()}")
            return "\n".join(lines)

        self.submit(f"卸载 {len(selection)} 个仓库", work, lambda ok, payload: (self.log(payload), self.scan_repos()))

    def normalize_selected(self) -> None:
        selection = self.require_selection()
        if not selection:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"把选中的 {len(selection)} 个仓库里的 mock_ai note 改写为配置的工具/模型？\n"
            "（真实写回 git note，可先用命令行 --dry-run 预览）",
        ):
            return

        def work():
            lines = [f"$ normalize --apply --repo {repo}" for repo in selection]
            for repo in selection:
                code, text = self.call(["normalize", "--repo", repo, "--apply"])
                lines.append(f"--- {repo} (exit {code}) ---\n{text.strip()}")
            return "\n".join(lines)

        self.submit("归一化 note", work, lambda ok, payload: (self.log(payload), self.scan_repos()))

    def repair_selected(self) -> None:
        selection = self.require_selection()
        if not selection:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"为选中的 {len(selection)} 个仓库扫描并重建「没有行归属」的 note？\n"
            "（按 commit diff 生成归属行，写入 tool/model，可能耗时长）",
        ):
            return

        def work():
            lines = []
            for repo in selection:
                code, text = self.call(["repair-empty", "--repo", repo, "--scan", "--apply"])
                lines.append(f"--- {repo} (exit {code}) ---\n{text.strip()}")
            return "\n".join(lines)

        self.submit("修复空 note", work, lambda ok, payload: (self.log(payload), self.scan_repos()))

    def backfill_selected(self) -> None:
        """给"完全没有 AI 记录"的历史提交补上归属（装 git-ai 之前的提交）。"""
        selection = self.require_selection()
        if not selection:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"为选中的 {len(selection)} 个仓库补全历史？\n\n"
            f"只处理「没有任何 AI 记录」的提交（比如装 git-ai 之前的提交），"
            f"已有记录不会被改动。\n"
            f"每条按 commit diff 生成行归属，写入当前配置的工具/模型。\n\n"
            f"每个仓库最多处理 {BACKFILL_LIMIT} 条，历史很长时可以多点几次（幂等）。\n"
            f"想先看条数：命令行 repair-empty --repo <路径> --scan-missing",
        ):
            return

        def work():
            lines: list[str] = []
            for repo in selection:
                code, text = self.call([
                    "repair-empty", "--repo", repo, "--scan-missing",
                    "--apply", "--limit", str(BACKFILL_LIMIT),
                ])
                lines.append(f"--- {repo} (exit {code}) ---\n{text.strip()}")
            return "\n".join(lines)

        self.submit(f"补全历史 {len(selection)} 个仓库", work,
                    lambda ok, payload: (self.log(payload), self.scan_repos()))

    def diagnose_selected(self) -> None:
        """把「为什么没算成 AI」的诊断结果列出来，方便发给别人看。"""
        selection = self.require_selection()
        if not selection:
            return

        def work():
            reports: list[dict] = []
            for repo in selection:
                data = self.call_json(["doctor", "--json", "--sample", "10", "--repo", repo])
                if data:
                    reports.extend(data)
            return reports

        def finish(ok, payload):
            if not ok or not payload:
                self.log("诊断失败。", "err")
                if self.last_raw_output.strip():
                    self.log(self.last_raw_output.rstrip())
                return
            text = self._format_doctor(payload)
            self.log(text)
            self._show_text("git-ai 诊断报告", text)

        self.submit(f"诊断 {len(selection)} 个仓库", work, finish)

    def _format_doctor(self, reports: list[dict]) -> str:
        labels = getattr(self.manager, "NOTE_KIND_LABEL", {})
        icons = {"ok": "[ok]  ", "warn": "[警告]", "fail": "[问题]", "info": "[信息]"}
        lines: list[str] = []
        for report in reports:
            lines.append("=" * 72)
            lines.append(f"仓库 : {report.get('repo', '')}")
            lines.append(f"结论 : {report.get('verdict', '')}")
            lines.append("-" * 72)
            for check in report.get("checks", []):
                lines.append(f"{icons.get(check.get('level'), '[?]')} {check.get('title', '')}")
                if check.get("detail"):
                    lines.append(f"        {check['detail']}")
            lines.append("-" * 72)
            head_kind = report.get("head_kind", "")
            lines.append(f"最近 {report.get('sample', 0)} 条提交：HEAD = {labels.get(head_kind, head_kind)}")
            for kind, count in sorted((report.get("distribution") or {}).items()):
                lines.append(f"  {labels.get(kind, kind):26} {count}")
            lines.append("")
        lines.append("说明：human_additions 占满 = 提交时没有 AI checkpoint（钩子没装 / daemon 没跑 / 跳过钩子）")
        lines.append("      历史 human note 可以用「归一化选中」或 convert-human --apply 转成 AI 会话")
        return "\n".join(lines)

    def _show_text(self, title: str, text: str) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.minsize(720, 480)
        frame = ttk.Frame(dialog, padding=8)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        box = tk.Text(frame, wrap="none", width=96, height=28, font="TkFixedFont")
        box.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        box.configure(yscrollcommand=vsb.set)
        box.insert("1.0", text)
        box.configure(state="disabled")
        bar = ttk.Frame(frame)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bar.columnconfigure(0, weight=1)

        def copy_report() -> None:
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
                self.status_var.set("诊断报告已复制到剪贴板")
            except tk.TclError:
                pass

        ttk.Button(bar, text="复制报告", command=copy_report).grid(row=0, column=1, padx=4)
        ttk.Button(bar, text="关闭", command=dialog.destroy).grid(row=0, column=2)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        try:
            dialog.wait_visibility()
            dialog.grab_set()
            dialog.focus_set()
        except tk.TclError:
            pass
        try:
            self.wait_window(dialog)
        except tk.TclError:
            pass

    def edit_repo_tool(self) -> None:
        selection = self.selected_repos()
        if len(selection) != 1:
            messagebox.showinfo(APP_TITLE, "请选择（且只选）一个仓库。")
            return
        repo = selection[0]
        row = next((r for r in self.repo_rows if r.get("repo") == repo), {})
        dialog = tk.Toplevel(self)
        dialog.title("仓库工具/模型")
        dialog.transient(self)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text=repo, wraplength=460).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text="工具").grid(row=1, column=0, sticky="w", pady=(10, 2))
        tool_var = tk.StringVar(value=str(row.get("tool", "")))
        ttk.Entry(body, textvariable=tool_var, width=32).grid(row=1, column=1, sticky="ew", pady=(10, 2))
        ttk.Label(body, text="模型").grid(row=2, column=0, sticky="w", pady=2)
        model_var = tk.StringVar(value=str(row.get("model", "")))
        ttk.Entry(body, textvariable=model_var, width=32).grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Label(body, text="留空则沿用默认值；两个都留空 = 删除该仓库的单独配置",
                  foreground="#666").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def apply() -> None:
            tool = tool_var.get().strip()
            model = model_var.get().strip()
            if not tool and not model:
                argv = ["config", "remove-repo", repo]
            else:
                argv = ["config", "set-repo", repo]
                if tool:
                    argv += ["--tool", tool]
                if model:
                    argv += ["--model", model]
            code, text = self.call(argv)
            self.log(text.strip())
            dialog.destroy()
            self.load_config_into_ui()
            self.scan_repos()

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=apply).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="取消", command=dialog.destroy).grid(row=0, column=1)

        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: apply())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        try:
            dialog.update_idletasks()
            parent_x, parent_y = self.winfo_rootx(), self.winfo_rooty()
            dialog.geometry(f"+{parent_x + 60}+{parent_y + 60}")
            dialog.wait_visibility()
            dialog.grab_set()
            dialog.focus_set()
        except tk.TclError:
            pass
        try:
            self.wait_window(dialog)
        except tk.TclError:
            pass  # 窗口已被提前关闭

    # ------------------------------------------------------------ 同步页
    def _build_sync_tab(self) -> None:
        frame = self.tab_sync
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        picker = ttk.LabelFrame(frame, text="选择项目", padding=PAD)
        picker.grid(row=0, column=0, sticky="ew")
        picker.columnconfigure(1, weight=1)
        ttk.Label(picker, text="仓库").grid(row=0, column=0, padx=(0, 6))
        self.var_sync_repo = tk.StringVar()
        self.sync_combo = ttk.Combobox(picker, textvariable=self.var_sync_repo, state="readonly")
        self.sync_combo.grid(row=0, column=1, sticky="ew")
        ttk.Button(picker, text="刷新列表", command=self.refresh_sync_choices).grid(row=0, column=2, padx=4)
        ttk.Label(picker, text="只同步这个仓库的 refs/notes/ai（远端默认 origin）",
                  foreground="#666").grid(row=1, column=1, sticky="w", pady=(4, 0))

        options = ttk.LabelFrame(frame, text="选项", padding=PAD)
        options.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        self.var_sync_dry = tk.BooleanVar(value=True)
        self.var_sync_normalize = tk.BooleanVar(value=True)
        self.var_sync_push = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="试运行（不改动、不推送）",
                        variable=self.var_sync_dry).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options, text="推送前先归一化 mock_ai / 本地 human note",
                        variable=self.var_sync_normalize).grid(row=0, column=1, sticky="w", padx=12)
        ttk.Checkbutton(options, text="推送远端", variable=self.var_sync_push).grid(row=0, column=2, sticky="w", padx=12)

        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, sticky="ew", pady=PAD)
        ttk.Button(actions, text="同步此项目", command=self.sync_selected).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(actions, text="同步全部仓库", command=self.sync_all).grid(row=0, column=1, padx=6)

        columns = [
            ("name", "仓库", 170, "w", False),
            ("result", "结果", 150, "w", False),
            ("normalized", "归一化", 80, "e", False),
            ("detail", "详情", 500, "w", True),
        ]
        holder, tree = self._make_table(frame, columns, height=8)
        holder.grid(row=3, column=0, sticky="nsew")
        self.trees["sync"] = tree

    def refresh_repo_choices(self) -> None:
        """扫描结束后刷新「同步」和「记录」两个页面的项目下拉框。"""
        names = [row.get("repo", "") for row in self.repo_rows]
        self.sync_combo["values"] = names
        if names and self.var_sync_repo.get() not in names:
            self.var_sync_repo.set(names[0])
        options = [RECORDS_ALL_LABEL] + names
        self.records_combo["values"] = options
        if self.var_records_repo.get() not in options:
            self.var_records_repo.set(names[0] if names else RECORDS_ALL_LABEL)

    # 兼容旧调用名
    refresh_sync_choices = refresh_repo_choices

    def _sync_argv(self, repo: str | None) -> list[str]:
        argv = ["sync-notes", "--json"]
        if repo:
            argv += ["--repo", repo]
        else:
            for index in range(self.roots_list.size()):
                argv += ["--root", self.roots_list.get(index)]
            argv += ["--depth", str(int(self.var_depth.get() or 4))]
        if self.var_sync_dry.get():
            argv.append("--dry-run")
        if not self.var_sync_normalize.get():
            argv.append("--no-normalize")
        if not self.var_sync_push.get():
            argv.append("--no-push")
        return argv

    def _finish_sync(self, ok, payload) -> None:
        tree = self.trees["sync"]
        tree.delete(*tree.get_children(""))
        if not ok or payload is None:
            self.log("同步失败，详见上方输出。", "err")
            if self.last_raw_output.strip():
                self.log(self.last_raw_output.rstrip())
            return
        for index, row in enumerate(payload):
            tree.insert("", "end", iid=str(index), values=(
                row.get("name", ""), row.get("result", ""),
                row.get("normalized", 0), row.get("detail", "")))
        self.log("同步完成。")
        self.scan_repos()

    def sync_selected(self) -> None:
        repo = self.var_sync_repo.get().strip()
        if not repo:
            messagebox.showinfo(APP_TITLE, "请先扫描仓库并选择一个项目。")
            return
        argv = self._sync_argv(repo)  # 主线程构造，避免后台线程读 Tk 变量

        def work():
            return self.call_json(argv) or []

        self.submit(f"同步 {Path(repo).name}", work, self._finish_sync)

    def sync_all(self) -> None:
        if not messagebox.askyesno(APP_TITLE, "同步所有源码目录下仓库的 notes？"):
            return
        argv = self._sync_argv(None)

        def work():
            return self.call_json(argv) or []

        self.submit("同步全部仓库", work, self._finish_sync)

    # ------------------------------------------------------------ 记录页
    def _build_records_tab(self) -> None:
        frame = self.tab_records
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(toolbar, text="项目").grid(row=0, column=0, padx=(0, 4))
        self.var_records_repo = tk.StringVar()
        self.records_combo = ttk.Combobox(toolbar, textvariable=self.var_records_repo,
                                          state="readonly", width=52)
        self.records_combo.grid(row=0, column=1)
        # 选中即加载：避免一次性拉取所有仓库导致卡顿
        self.records_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_records())
        ttk.Button(toolbar, text="刷新列表", command=self.scan_repos).grid(row=0, column=2, padx=4)
        ttk.Button(toolbar, text="刷新记录", command=self.load_records).grid(row=0, column=3, padx=2)
        ttk.Label(toolbar, text="每个项目").grid(row=0, column=4, padx=(12, 4))
        self.var_limit = tk.IntVar(value=20)
        ttk.Spinbox(toolbar, from_=1, to=500, width=5, textvariable=self.var_limit).grid(row=0, column=5)
        ttk.Label(toolbar, text="条").grid(row=0, column=6, padx=4)

        self.records_hint = ttk.Label(
            frame,
            text="先在「仓库」页扫描，然后在这里选择项目查看提交记录（默认只查这一个仓库）",
            foreground="#666",
        )
        self.records_hint.grid(row=1, column=0, sticky="w", pady=(0, 4))

        columns = [
            ("repo", "仓库", 160, "w", False),
            ("short", "提交", 90, "w", False),
            ("subject", "标题", 420, "w", True),
            ("lines", "AI 行", 60, "e", False),
            ("tools", "工具::模型", 180, "w", False),
            ("remote", "远端", 90, "w", False),
            ("author", "作者", 120, "w", False),
        ]
        holder, tree = self._make_table(frame, columns, height=15)
        holder.grid(row=2, column=0, sticky="nsew")
        self.trees["records"] = tree

    def load_records(self) -> None:
        if not self.repo_rows:
            self.status_var.set("请先到「仓库」页扫描仓库")
            self.log("记录页需要先扫描仓库（仓库 → 扫描仓库）", "err")
            return
        choice = self.var_records_repo.get().strip()
        limit = str(int(self.var_limit.get() or 20))
        argv = ["records", "--json", "--limit", limit]
        if choice and choice != RECORDS_ALL_LABEL:
            argv += ["--repo", choice]
            label = f"读取记录（{Path(choice).name}）"
        else:
            for index in range(self.roots_list.size()):
                argv += ["--root", self.roots_list.get(index)]
            argv += ["--depth", str(int(self.var_depth.get() or 4))]
            label = "读取记录（全部仓库）"

        def work():
            return self.call_json(argv)

        def finish(ok, payload):
            tree = self.trees["records"]
            tree.delete(*tree.get_children(""))
            if not ok or payload is None:
                self.log("读取记录失败。", "err")
                if self.last_raw_output.strip():
                    self.log(self.last_raw_output.rstrip())
                return
            for index, row in enumerate(payload):
                tree.insert("", "end", iid=str(index), values=(
                    Path(row.get("repo", "")).name,
                    row.get("short", ""),
                    row.get("subject", ""),
                    row.get("ai_lines", 0),
                    row.get("tools", "") or "-",
                    row.get("remote", ""),
                    row.get("author", ""),
                ))
            scope = "全部仓库" if not choice or choice == RECORDS_ALL_LABEL else Path(choice).name
            self.records_hint.configure(text=f"当前：{scope} · 每仓库最多 {limit} 条 · 共 {len(payload)} 条")
            self.status_var.set(f"共 {len(payload)} 条提交记录")

        self.submit(label, work, finish)

    # ------------------------------------------------------------ 日志页
    def _build_logs_tab(self) -> None:
        frame = self.tab_logs
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="刷新日志", command=self.load_logs).grid(row=0, column=0, padx=2)
        ttk.Label(toolbar, text="显示最近").grid(row=0, column=1, padx=(12, 4))
        self.var_tail = tk.IntVar(value=100)
        ttk.Spinbox(toolbar, from_=1, to=5000, width=6, textvariable=self.var_tail).grid(row=0, column=2)
        ttk.Label(toolbar, text="条").grid(row=0, column=3, padx=4)
        self.var_logs_all = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="包含历史日志文件", variable=self.var_logs_all).grid(
            row=0, column=4, padx=12)

        columns = [
            ("ts", "时间", 150, "w", False),
            ("repo", "仓库", 140, "w", False),
            ("commit", "提交", 90, "w", False),
            ("action", "动作", 110, "w", False),
            ("status", "状态", 90, "w", False),
            ("tools", "工具", 130, "w", False),
            ("detail", "详情", 500, "w", True),
        ]
        holder, tree = self._make_table(frame, columns, height=15)
        holder.grid(row=1, column=0, sticky="nsew")
        self.trees["logs"] = tree

    def load_logs(self) -> None:
        argv = ["logs", "--json", "--tail", str(int(self.var_tail.get() or 100))]
        if self.var_logs_all.get():
            argv.append("--all")

        def work():
            return self.call_json(argv)

        def finish(ok, payload):
            tree = self.trees["logs"]
            tree.delete(*tree.get_children(""))
            if not ok or payload is None:
                self.log("读取日志失败。", "err")
                return
            for index, row in enumerate(payload):
                detail = row.get("detail") or row.get("repo_path") or ""
                tree.insert("", "end", iid=str(index), values=(
                    str(row.get("ts", "")).replace("T", " "),
                    row.get("repo", ""),
                    str(row.get("commit", ""))[:12],
                    row.get("action", ""),
                    row.get("status", ""),
                    (row.get("tool", "") + ("::" + row["model"] if row.get("model") else "")).strip(":"),
                    detail,
                ))
            self.status_var.set(f"共 {len(payload)} 条日志")

        self.submit("读取日志", work, finish)

    # ------------------------------------------------------------ 生命周期
    def on_close(self) -> None:
        try:
            self.settings.data["geometry"] = self.geometry()
            self.settings.data["lastTab"] = self.notebook.index(self.notebook.select())
            self.settings.save()
        except Exception:
            pass
        finally:
            self.destroy()


def run_cli(argv: list[str]) -> int:
    """命令行模式（打包后同一个 exe 也用它）。"""
    manager = load_manager_module(script_dir() / MANAGER_FILENAME)
    manager.ensure_streams()
    if argv and argv[0] == "--selftest":
        return selftest()
    return manager.main(argv)


def selftest() -> int:
    """隐藏窗口跑一遍环境检测 + 扫描，用来验证打包结果是否可用。"""
    import time

    tk.Tk.deiconify = lambda self: None  # type: ignore[method-assign]
    app = App()

    def wait_idle(seconds: float, settle: float = 0.6) -> None:
        """等后台任务结束；settle 让任务先登记为 busy，避免误判为已空闲。"""
        time.sleep(settle)
        deadline = time.time() + seconds
        while time.time() < deadline:
            app.update()
            time.sleep(0.05)
            if not app.busy:
                break

    try:
        wait_idle(90)
        print(f"[selftest] 运行方式 : {'独立 exe' if getattr(sys, 'frozen', False) else '源码'}")
        print(f"[selftest] home     : {app.home}")
        print(f"[selftest] manager  : {app.manager_path}")
        print("[selftest] Python 选项: " + ("已隐藏（独立 exe 不需要 Python）"
                                            if getattr(sys, "frozen", False)
                                            else app.manager.find_python()))
        print("[selftest] Python 提示: " + str(app.python_hint.cget("text"))[:60])
        print(f"[selftest] git-ai   : {app.manager.find_git_ai()}")
        app.scan_repos()
        wait_idle(300)
        rows = app.trees["repos"].get_children("")
        if not rows and app.roots_list.size():
            print("[selftest] 首次扫描为空，重试一次 ...")
            app.scan_repos()
            wait_idle(300)
            rows = app.trees["repos"].get_children("")
        print(f"[selftest] 扫描     : {len(rows)} 个仓库")
        for item in rows[:5]:
            values = app.trees["repos"].item(item, "values")
            print(f"            - {values[0]}  notes={values[3]} hook={values[8]}")
        return 0
    finally:
        try:
            app.destroy()
        except tk.TclError:
            pass


def main() -> int:
    argv = sys.argv[1:]
    try:
        if argv:
            return run_cli(argv)
        try:
            app = App()
        except tk.TclError as exc:
            sys.stderr.write(f"无法启动图形界面：{exc}\n")
            return 1
        app.mainloop()
        return 0
    except SystemExit as exc:
        # 同上：字符串消息代表出错，必须非 0
        if isinstance(exc.code, int):
            return exc.code
        if exc.code:
            try:
                sys.stderr.write(str(exc.code) + "\n")
            except Exception:
                pass
            return 1
        return 0
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
