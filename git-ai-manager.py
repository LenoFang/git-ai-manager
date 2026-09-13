#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-ai-manager - one cross-platform CLI for the git-ai authorship workflow.

It replaces the scattered PowerShell helpers with a single entry point:

    hook pre-commit    <- .git/hooks/pre-commit     staged files -> mock_ai checkpoint
    hook post-commit   <- .git/hooks/post-commit
                          commit_sha -> repository path map, then normalizes
                          the note of the previous commit
    notes-updated      <- git-ai post_notes_updated mock_ai / human notes -> tool + model

    install / uninstall  write or remove the hooks above for every repo under a root
    config / init        manage this tool's own configuration file

    sync-notes           normalize, then fetch + merge + push refs/notes/ai
    refresh-notes        re-apply normalization to every note of a repository

    normalize            rewrite historical "tool":"mock_ai" notes
    convert-human        rewrite historical human notes into codex sessions
    repair-empty         rebuild line attribution for notes that have none
    status / records / logs

Nothing machine specific is hard-coded.  Every location is discovered at run time:

    git-ai home ....... $GIT_AI_HOME, else the directory that holds this file
    git-ai binary ..... $GIT_AI_EXE, else PATH, else <home>/bin/git-ai[.exe]
    python for hooks .. --python, $GIT_AI_PYTHON, sys.executable, else PATH
    project roots ..... --root, $GIT_AI_PROJECT_ROOTS, else config "projectsRoots",
                        else the repository that contains the current directory
    tool / model ...... config "repos" (per repository) -> config "defaults"

Two git-ai 1.7.x behaviours shape this design:

  * Its own post_notes_updated hook is fired from the "git core hooks" code
    path, which upstream has sunset: it only runs when git-ai's hooks are
    installed through core.hooksPath.  On a normal install it never fires, so
    the commit hook and sync-notes normalize notes themselves.
  * The daemon publishes a note for a commit only after the git process exits,
    so a commit hook can never see its own note.  post-commit therefore
    normalizes the previous commit, sync-notes catches the last one before it
    is pushed, and "normalize" repairs older history in bulk.

Set GIT_AI_MANAGER_DEBUG=1 to trace every git/git-ai call with timings.

Requires Python 3.10+.  Pure standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

APP = "git-ai-manager"

# ---------------------------------------------------------------- environment
ENV_HOME = "GIT_AI_HOME"
ENV_GIT_AI_EXE = "GIT_AI_EXE"
ENV_PYTHON = "GIT_AI_PYTHON"
ENV_CONFIG = "GIT_AI_MANAGER_CONFIG"
ENV_ROOTS = "GIT_AI_PROJECT_ROOTS"
ENV_AUTO = "GIT_AI_AUTO_AI"
ENV_DEBUG = "GIT_AI_MANAGER_DEBUG"

NOTES_REFNAME = "refs/notes/ai"
REMOTE_SNAPSHOT_REF = "refs/git-ai-manager/remote-notes-ai"

CONFIG_NAME = "manager.json"
SOURCE_FILENAME = "git-ai-manager.py"
EXAMPLE_NAME = "manager.example.json"
MAP_DIR_NAME = "notes-repo-map"
LOG_DIR_NAME = "manager-logs"

GIT_AI_HOOK_KEY = "git_ai_hooks.post_notes_updated"

BLOCK_BEGIN = "# BEGIN git-ai manager block"
BLOCK_END = "# END git-ai manager block"
LEGACY_MARKERS = ("precommit-ai.ps1", "postcommit-ai.ps1", "notes-updated-ai.ps1")
HOOK_KINDS = ("pre-commit", "post-commit")

DEFAULT_REPO_DEPTH = 4
DEFAULT_BACKFILL_LIMIT = 200

# Directories that are never repositories themselves and are expensive to walk.
PRUNE_DIR_NAMES = frozenset(
    {
        ".git", "node_modules", "target", "build", "dist", "out", "obj", "bin",
        ".venv", "venv", "env", "__pycache__", ".idea", ".vs", ".vscode",
        "vendor", "third_party", "Pods", "DerivedData", ".gradle", ".m2",
        "coverage", ".next", ".nuxt", ".cache", ".tox", ".pytest_cache",
        "site-packages", ".terraform", ".serverless", ".parcel-cache",
    }
)

DEFAULT_WHEN_UNKNOWN = "unknown"


# ---------------------------------------------------------------- small utils
def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def info(msg: str = "") -> None:
    print(msg)


def warn(msg: str) -> None:
    eprint(f"[{APP}] {msg}")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def expand(value: object) -> Path:
    """Expand ~ and $VARS in a user supplied path."""
    text = os.path.expandvars(str(value))
    text = os.path.expanduser(text)
    return Path(text)


def abs_posix(p: object) -> str:
    """Absolute path with forward slashes - safe for sh hooks on every OS."""
    return os.path.abspath(str(expand(p))).replace("\\", "/")


def norm_key(p: object) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(expand(p)))))


def same_path(a: object, b: object) -> bool:
    try:
        if norm_key(a) == norm_key(b):
            return True
        return os.path.normcase(os.path.realpath(str(expand(a)))) == os.path.normcase(
            os.path.realpath(str(expand(b)))
        )
    except OSError:
        return False


def is_within(child: Path, parent: Path) -> bool:
    try:
        child_n = norm_key(child)
        parent_n = norm_key(parent).rstrip("\\/") + os.sep
        return child_n == parent_n.rstrip("\\/") or child_n.startswith(parent_n)
    except OSError:
        return False


def dwidth(text: str) -> int:
    """Display width, counting CJK characters as two columns."""
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - dwidth(text))


def trunc(text: str, width: int) -> str:
    text = str(text)
    if dwidth(text) <= width:
        return text
    out = ""
    used = 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > width - 1:
            break
        out += ch
        used += step
    return out + "\u2026"


def render_table(headers: list[str], rows: list[list[object]]) -> str:
    cols = len(headers)
    widths = [dwidth(h) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], dwidth(str(cell)))
    lines = ["  ".join(pad(h, widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(cols)))
    for row in rows:
        cells = [(row[i] if i < len(row) else "") for i in range(cols)]
        lines.append("  ".join(pad(str(c), widths[i]) for i, c in enumerate(cells)).rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------- locations
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Where the program's own files live (a temporary unpack dir when frozen)."""
    return Path(__file__).resolve().parent


def _bundled_source_root(exe: Path) -> Path | None:
    """Recognise a development layout: <tool>/dist/<build>/git-ai-manager.exe.

    When the sources sit next to the build folder, the tool folder is used as
    home so that the exe and the source share one configuration, log and repo
    map instead of silently creating a second, empty one.
    """
    for parent in list(exe.parents)[:3]:
        try:
            if (parent / SOURCE_FILENAME).is_file():
                return parent
        except OSError:
            continue
    return None


def app_dir() -> Path:
    """The folder the user sees: next to the executable when frozen.

    Frozen builds unpack into a temp directory that disappears on exit, so the
    configuration, logs and repository map must never be resolved from there.
    On macOS the executable sits inside <name>.app/Contents/MacOS, and writing
    user data into the bundle is wrong, so the folder *containing* the .app is
    used instead.
    """
    if is_frozen():
        try:
            exe = Path(sys.executable).resolve()
        except OSError:
            return bundle_dir()
        dev_root = _bundled_source_root(exe)
        if dev_root is not None:
            return dev_root
        parts = exe.parts
        lowered = [part.lower() for part in parts]
        if "contents" in lowered and "macos" in lowered:
            index = lowered.index("contents")
            if index > 0 and parts[index - 1].endswith(".app"):
                return Path(*parts[: index - 1])
        return exe.parent
    return bundle_dir()


_DIAGNOSTIC_STREAM = None


def diagnostic_stream():
    """Fallback stream for builds without a console (windowed exe).

    Output and tracebacks would otherwise be discarded, so they are appended to
    manager-logs/manager-<date>.log next to the executable.
    """
    global _DIAGNOSTIC_STREAM
    if _DIAGNOSTIC_STREAM is not None:
        return _DIAGNOSTIC_STREAM
    try:
        directory = app_dir() / LOG_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"manager-{datetime.now().strftime('%Y%m%d')}.log"
        _DIAGNOSTIC_STREAM = open(target, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        _DIAGNOSTIC_STREAM = open(os.devnull, "w", encoding="utf-8")
    return _DIAGNOSTIC_STREAM


def _stream_ok(stream) -> bool:
    """A stream is usable when it exists and is not the null device."""
    if stream is None:
        return False
    try:
        name = getattr(stream, "name", None)
        if isinstance(name, str) and name:
            if name.lower() in ("nul", "/dev/null", "nul:", "os.devnull"):
                return False
            if os.path.exists(name) and os.path.samefile(name, os.devnull):
                return False
    except (OSError, ValueError):
        pass
    return True


def _windows_handle_stream(index: int):
    """Wrap an inherited Windows std handle (0=in, 1=out, 2=err) as text."""
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10 - index)
        if not handle or handle in (-1, 0):
            return None
        if kernel32.GetFileType(handle) == 0:  # FILE_TYPE_UNKNOWN
            return None
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if index == 0 else os.O_WRONLY)
        return io.open(fd, "r" if index == 0 else "w", encoding="utf-8",
                       errors="replace", buffering=1)
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def ensure_streams() -> None:
    """Make sure stdout/stderr actually work in a windowed build.

    A PyInstaller windowed exe starts with its streams pointed at the null
    device, so every print() disappears.  Recover the inherited OS handles
    (that keeps `git-ai-manager.exe status > out.txt` and pipelines working),
    otherwise attach to the parent console, and only then fall back to a log
    file so that failures stay diagnosable.
    """
    if not (_stream_ok(sys.stdout) and _stream_ok(sys.stderr)):
        if os.name == "nt":
            if not _stream_ok(sys.stdout):
                sys.stdout = _windows_handle_stream(1) or sys.stdout
            if not _stream_ok(sys.stderr):
                sys.stderr = _windows_handle_stream(2) or sys.stderr
            if not _stream_ok(sys.stdin):
                sys.stdin = _windows_handle_stream(0) or sys.stdin
            if not (_stream_ok(sys.stdout) and _stream_ok(sys.stderr)):
                try:
                    import ctypes

                    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                    if kernel32.AttachConsole(0xFFFFFFFF):  # ATTACH_PARENT_PROCESS
                        for name in ("stdout", "stderr"):
                            try:
                                setattr(sys, name, open("CONOUT$", "w", encoding="utf-8",
                                                        errors="replace", buffering=1))
                            except OSError:
                                pass
                except Exception:
                    pass
        for name in ("stdout", "stderr"):
            if not _stream_ok(getattr(sys, name)):
                try:
                    setattr(sys, name, diagnostic_stream())
                except OSError:
                    pass
    # 统一成 UTF-8，否则中文经过管道/重定向会按 ANSI 代码页编码而乱码
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass


def script_dir() -> Path:
    return bundle_dir()


def manager_script() -> Path:
    """The thing hooks should call: the .py normally, the exe when frozen."""
    if is_frozen():
        try:
            return Path(sys.executable).resolve()
        except OSError:
            pass
    return Path(__file__).resolve()


def home_dir() -> Path:
    override = os.environ.get(ENV_HOME)
    if override and override.strip():
        return expand(override)
    return app_dir()


def config_file() -> Path:
    override = os.environ.get(ENV_CONFIG)
    if override and override.strip():
        return expand(override)
    return home_dir() / CONFIG_NAME


def repo_map_dir() -> Path:
    return home_dir() / MAP_DIR_NAME


def log_dir() -> Path:
    return home_dir() / LOG_DIR_NAME


def canonical(path: object) -> str:
    """Return the path spelling exactly as it exists on disk.

    git-ai.exe silently degrades into a plain `git` passthrough when it is
    started through a differently cased name, and shutil.which() on Windows
    hands back "git-ai.EXE" (upper case, taken from PATHEXT).  The real
    spelling therefore decides whether AI attribution works at all.
    """
    try:
        return os.path.realpath(str(path))
    except OSError:
        return str(path)


GIT_AI_EXE_NAMES = ("git-ai.exe", "git-ai")


def git_ai_search_paths() -> list[tuple[Path, str]]:
    """Every place the git-ai executable is looked for, in priority order.

    Returns (path, "为什么找这里") so the GUI and `doctor` can explain what was
    tried instead of just saying "not found".
    """
    found: list[tuple[Path, str]] = []

    override = os.environ.get(ENV_GIT_AI_EXE)
    if override and override.strip():
        found.append((expand(override), f"环境变量 {ENV_GIT_AI_EXE}"))

    on_path = shutil.which("git-ai")
    if on_path:
        found.append((Path(on_path), "PATH"))

    # 工具自己的 bin/ —— 分发时可以把 git-ai.exe 一起放进来，做成便携包
    for name in GIT_AI_EXE_NAMES:
        found.append((home_dir() / "bin" / name, "工具目录下的 bin/"))

    user_home = Path.home()
    for relative, label in (
        (".git-ai/bin", "~/.git-ai/bin（git-ai 默认安装位置）"),
        (".local/bin", "~/.local/bin"),
        (".cargo/bin", "~/.cargo/bin"),
        ("bin", "~/bin"),
    ):
        for name in GIT_AI_EXE_NAMES:
            found.append((user_home / relative / name, label))

    if os.name == "nt":
        for variable, label in (("LOCALAPPDATA", "%LOCALAPPDATA%"),
                                ("APPDATA", "%APPDATA%"),
                                ("ProgramFiles", "%ProgramFiles%"),
                                ("ProgramFiles(x86)", "%ProgramFiles(x86)%")):
            base = os.environ.get(variable)
            if not base:
                continue
            for sub in ("git-ai", "Programs/git-ai", "git-ai/bin"):
                for name in GIT_AI_EXE_NAMES:
                    found.append((Path(base) / sub / name, f"{label}\\{sub}"))
    else:
        for folder in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/opt/local/bin"):
            found.append((Path(folder) / "git-ai", folder))

    return found


def find_git_ai_with_origin() -> tuple[str | None, str]:
    """(executable, where it was found) - the second value is human readable."""
    seen: set[str] = set()
    for candidate, origin in git_ai_search_paths():
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        key = norm_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        return canonical(candidate), origin
    return None, ""


def find_git_ai() -> str | None:
    """Locate the git-ai executable without assuming any fixed install path."""
    return find_git_ai_with_origin()[0]


# Interpreter layouts that live inside the git-ai manager folder.  Using one of
# these keeps the installed hooks independent of whatever python happens to be
# on PATH (and of a system python that may later be upgraded away).
IN_FOLDER_PYTHON_PATHS = (
    (".venv", "Scripts", "python.exe"),
    (".venv", "bin", "python3"),
    (".venv", "bin", "python"),
    ("venv", "Scripts", "python.exe"),
    ("venv", "bin", "python3"),
    ("venv", "bin", "python"),
    ("python", "python.exe"),
    ("python", "bin", "python3"),
    ("python", "bin", "python"),
    ("runtime", "python.exe"),
    ("runtime", "bin", "python3"),
    ("runtime", "bin", "python"),
)

_PYTHON_PROBE: dict[str, bool] = {}


def probe_python(path: object) -> bool:
    """Check that an interpreter really runs (a stale venv must not win)."""
    key = norm_key(path)
    if key in _PYTHON_PROBE:
        return _PYTHON_PROBE[key]
    try:
        result = run([str(path), "-c", "import sys;print(sys.version_info[0])"], timeout=30)
        ok = result.returncode == 0 and (result.stdout or "").strip().startswith("3")
    except Exception:
        ok = False
    _PYTHON_PROBE[key] = ok
    return ok


def local_python() -> str | None:
    """Interpreter shipped inside the git-ai manager folder, if any."""
    for parts in IN_FOLDER_PYTHON_PATHS:
        candidate = home_dir().joinpath(*parts)
        try:
            if candidate.is_file() and probe_python(candidate):
                return str(candidate)
        except OSError:
            continue
    return None


def resolve_python(value: object) -> str | None:
    text = str(value)
    if not text.strip():
        return None
    if os.sep in text or "/" in text:
        path = expand(text)
        try:
            if path.is_file():
                return str(path)
        except OSError:
            return None
        return None
    return shutil.which(text)


def find_python(override: str | None = None) -> str:
    """Resolve the interpreter the installed hooks should call.

    Order: --python / $GIT_AI_PYTHON, then an interpreter inside the manager
    folder, then the running interpreter, then PATH.
    """
    for value in (override, os.environ.get(ENV_PYTHON)):
        if value:
            found = resolve_python(value)
            if found:
                return found
    local = local_python()
    if local:
        return local
    try:
        if sys.executable and Path(sys.executable).is_file():
            return sys.executable
    except OSError:
        pass
    for name in ("python3", "python", "py"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable or "python3"


def resolve_config_path(value: object) -> Path:
    """Relative entries in manager.json are resolved next to manager.json."""
    path = expand(value)
    if not path.is_absolute():
        path = config_file().parent / path
    return path


# ---------------------------------------------------------------- logging
def log_event(
    action: str,
    *,
    repo: object | None = None,
    commit: str | None = None,
    tool: str | None = None,
    model: str | None = None,
    status: str = "ok",
    detail: str | None = None,
    count: int | None = None,
) -> None:
    """Append one JSONL record.  Logging must never break the caller."""
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        record: dict[str, object] = {"ts": now_iso(), "action": action, "status": status}
        if repo is not None:
            record["repo"] = Path(str(repo)).name
            record["repo_path"] = abs_posix(repo)
        if commit:
            record["commit"] = commit
        if tool:
            record["tool"] = tool
        if model:
            record["model"] = model
        if count is not None:
            record["count"] = count
        if detail:
            record["detail"] = detail
        target = directory / f"manager-{datetime.now().strftime('%Y%m%d')}.jsonl"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_logs(tail: int, include_all: bool = False) -> list[dict]:
    directory = log_dir()
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("manager-*.jsonl"))
    if not include_all:
        files = files[-1:]
    records: list[dict] = []
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    parsed["_file"] = path.name
                    records.append(parsed)
        except OSError:
            continue
    return records[-tail:] if tail > 0 else records


# ---------------------------------------------------------------- git / process
def base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_PAGER"] = "cat"
    env.setdefault("GIT_EDITOR", "true")
    return env


def _subprocess_kwargs() -> dict:
    """Windows: never let a GUI process allocate a console for its children.

    A windowed build has no console, so every console child (git, git-ai) would
    get a brand new console allocated - that costs ~200 ms per call and can
    flash a black window.  Output is captured through pipes anyway.
    """
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def run(
    cmd: list[str],
    *,
    cwd: object | None = None,
    stdin: str | None = None,
    timeout: float | None = 180,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command with captured output.

    quiet=True sends the child's output to files instead of pipes, so a child
    that leaves grandchildren holding the pipe cannot stall the caller.
    """
    started = time.time()
    out_handle = None
    err_handle = None
    extra = _subprocess_kwargs()
    try:
        if quiet:
            out_path = tempfile.mktemp(suffix=".out")
            err_path = tempfile.mktemp(suffix=".err")
            out_handle = open(out_path, "w+", encoding="utf-8", errors="replace")
            err_handle = open(err_path, "w+", encoding="utf-8", errors="replace")
            result = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                input=stdin,
                stdout=out_handle,
                stderr=err_handle,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=base_env(),
                **extra,
            )
            out_handle.close()
            err_handle.close()
            result = subprocess.CompletedProcess(
                cmd,
                result.returncode,
                Path(out_path).read_text(encoding="utf-8", errors="replace"),
                Path(err_path).read_text(encoding="utf-8", errors="replace"),
            )
        else:
            result = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=base_env(),
                **extra,
            )
    except FileNotFoundError as exc:
        result = subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(cmd, 124, "", f"timeout after {timeout}s")
    except OSError as exc:
        result = subprocess.CompletedProcess(cmd, 126, "", str(exc))
    finally:
        for handle in (out_handle, err_handle):
            try:
                if handle is not None and not handle.closed:
                    handle.close()
            except OSError:
                pass
    if os.environ.get(ENV_DEBUG):
        eprint(f"[debug] {' '.join(cmd)}  -> exit {result.returncode} in {time.time() - started:.2f}s")
    return result


def git(args: list[str], repo: object | None = None, *, stdin: str | None = None,
        timeout: float | None = 180) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += list(args)
    return run(cmd, stdin=stdin, timeout=timeout)


def git_text(args: list[str], repo: object | None = None, default: str | None = None) -> str | None:
    result = git(args, repo)
    if result.returncode != 0:
        return default
    return result.stdout


def git_line(args: list[str], repo: object | None = None, default: str | None = None) -> str | None:
    text = git_text(args, repo)
    if text is None:
        return default
    stripped = text.strip()
    return stripped or default


def repo_root_of(path: object) -> Path | None:
    """Walk upwards until a git working tree root is found."""
    try:
        current = Path(str(path)).absolute()
    except OSError:
        return None
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        marker = candidate / ".git"
        if marker.exists():
            return candidate
    return None


def is_repo(path: object) -> bool:
    try:
        return (Path(str(path)) / ".git").exists()
    except OSError:
        return False


def current_repo() -> Path | None:
    top = git_line(["rev-parse", "--show-toplevel"], default=None)
    if top:
        return Path(top)
    return repo_root_of(Path.cwd())


def find_repos(
    roots: list[Path],
    *,
    depth: int = 1,
    include_worktrees: bool = False,
    max_entries: int = 20000,
) -> list[Path]:
    """Find git repositories under each root, at most `depth` levels below it.

    depth=1 looks at the immediate children only (the historic behaviour); an
    explicit depth (config "defaults.repoDepth", default 4) also walks nested
    layouts.  A directory that is itself a repository is never descended into,
    so a project holding several .git folders stays one repository.

    Linked worktrees are skipped on purpose: their commits are already covered
    by the main repository hooks.
    """
    found: list[Path] = []
    for root in roots:
        root = expand(root)
        if not root.is_dir():
            continue
        if (root / ".git").is_dir() or (include_worktrees and (root / ".git").exists()):
            found.append(root)
            continue
        queue: list[tuple[Path, int]] = []
        try:
            queue = [
                (child, 1)
                for child in sorted(root.iterdir(), key=lambda p: p.name)
                if child.is_dir()
            ]
        except OSError:
            continue
        visited = 0
        index = 0
        while index < len(queue) and visited < max_entries:
            current, level = queue[index]
            index += 1
            visited += 1
            marker = current / ".git"
            if marker.is_dir() or (include_worktrees and marker.exists()):
                found.append(current)
                continue
            if level >= depth or current.name in PRUNE_DIR_NAMES:
                continue
            try:
                for child in sorted(current.iterdir(), key=lambda p: p.name):
                    if child.is_dir() and child.name not in PRUNE_DIR_NAMES:
                        queue.append((child, level + 1))
            except OSError:
                continue
    unique: dict[str, Path] = {}
    for path in found:
        unique.setdefault(norm_key(path), path)
    return [unique[key] for key in sorted(unique)]


# ---------------------------------------------------------------- configuration
def default_config() -> dict:
    return {
        "projectsRoots": [],
        "defaults": {
            "tool": "codex",
            "model": DEFAULT_WHEN_UNKNOWN,
            "syncNotes": True,
            "convertLocalHuman": True,
            "normalizeOnCommit": True,
            "repoDepth": DEFAULT_REPO_DEPTH,
            "remote": "origin",
        },
        "repos": {},
    }


def strip_json_noise(text: str) -> str:
    """Tolerate // and /* */ comments plus trailing commas in manager.json."""
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    escaped = False
    while i < length:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            while i < length and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == ",":
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            if j < length and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def merge_config(base: dict, user: dict) -> dict:
    result = json.loads(json.dumps(base))
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path | None = None) -> dict:
    target = Path(path) if path else config_file()
    config = default_config()
    if not target.is_file():
        return config
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(f"{APP}: cannot read {target}: {exc}")
    if not raw.strip():
        return config
    try:
        user = json.loads(strip_json_noise(raw))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{APP}: {target} is not valid JSON: {exc}")
    if isinstance(user, dict):
        config = merge_config(config, user)
    return config


def save_config(config: dict, path: Path | None = None) -> Path:
    target = Path(path) if path else config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    os.replace(tmp, target)
    return target


def deep_get(config: dict, dotted: str):
    node: object = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def deep_set(config: dict, dotted: str, value: object) -> None:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def deep_unset(config: dict, dotted: str) -> bool:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            return False
        node = node[part]
    if parts[-1] in node:
        del node[parts[-1]]
        return True
    return False


def resolve_tool_model(config: dict, repo: object) -> tuple[str, str]:
    """Per repository tool/model wins, then defaults."""
    entry: dict = {}
    repos = config.get("repos")
    if isinstance(repos, dict) and repo is not None:
        repo_path = expand(repo)
        best: tuple[int, dict] | None = None
        for key, value in repos.items():
            if not isinstance(value, dict):
                continue
            if same_path(key, repo_path):
                entry = value
                best = None
                break
            configured = resolve_config_path(key)
            if is_within(repo_path, configured):
                depth = len(str(configured))
                if best is None or depth > best[0]:
                    best = (depth, value)
        if best is not None:
            entry = best[1]
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    tool = entry.get("tool") if entry.get("tool") else defaults.get("tool")
    model = entry.get("model") if entry.get("model") else defaults.get("model")
    return (
        str(tool) if tool else "codex",
        str(model) if model is not None and model != "" else DEFAULT_WHEN_UNKNOWN,
    )


def resolve_remote(config: dict, override: str | None = None) -> str:
    if override:
        return override
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    remote = defaults.get("remote")
    return str(remote) if remote else "origin"


def explicit_roots(args: argparse.Namespace, config: dict) -> list[Path]:
    values: list[str] = list(getattr(args, "root", None) or [])
    if not values:
        env_roots = os.environ.get(ENV_ROOTS)
        if env_roots:
            values += [p for p in env_roots.split(os.pathsep) if p.strip()]
    if not values:
        values += [str(v) for v in (config.get("projectsRoots") or [])]
    roots: list[Path] = []
    for value in values:
        path = resolve_config_path(value)
        if path.is_dir():
            roots.append(path)
        else:
            warn(f"projects root not found, skipped: {path}")
    unique: dict[str, Path] = {}
    for path in roots:
        unique.setdefault(norm_key(path), path)
    return list(unique.values())


def resolve_depth(args: argparse.Namespace, config: dict) -> int:
    value = getattr(args, "depth", None)
    if value is None:
        defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
        value = defaults.get("repoDepth", DEFAULT_REPO_DEPTH)
    try:
        depth = int(value)
    except (TypeError, ValueError):
        depth = DEFAULT_REPO_DEPTH
    return max(1, min(depth, 32))


def collect_repos(args: argparse.Namespace, config: dict) -> list[Path]:
    repos: list[Path] = []
    explicit = getattr(args, "repo", None)
    if explicit:
        candidate = expand(explicit)
        if not is_repo(candidate):
            raise SystemExit(f"{APP}: not a git repository: {candidate}")
        repos.append(candidate)
    # An explicit --repo means "only this repository" unless roots were given too.
    only_repo = bool(explicit) and not (getattr(args, "root", None) or os.environ.get(ENV_ROOTS))
    if not only_repo:
        roots = explicit_roots(args, config)
        if roots:
            repos += find_repos(roots, depth=resolve_depth(args, config))
    if not repos:
        fallback = current_repo()
        if fallback:
            repos = [fallback]
    unique: dict[str, Path] = {}
    for path in repos:
        if is_repo(path):
            unique.setdefault(norm_key(path), expand(path))
    if not unique:
        raise SystemExit(
            textwrap.dedent(
                f"""\
                {APP}: no git repository found.

                Point the tool at your projects one of these ways:
                  * --root <dir>            (repeatable)
                  * export {ENV_ROOTS}=<dir>{os.pathsep}<dir>
                  * {APP} config add-root <dir>     (persisted in manager.json)
                  * run the command from inside a repository
                """
            )
        )
    return [unique[key] for key in sorted(unique)]


# ---------------------------------------------------------------- note helpers
def split_note(text: str) -> tuple[str, str] | None:
    """Split an authorship note into (attribution block, json block)."""
    match = re.search(r"(?m)^---[ \t]*\r?\n", text)
    if not match:
        return None
    return text[: match.start()], text[match.end():]


def detect_indent(json_text: str) -> int | None:
    match = re.search(r"\r?\n( +)\"", json_text)
    if match:
        return len(match.group(1))
    return None


def dump_json_like(obj: object, source: str) -> str:
    return json.dumps(obj, indent=detect_indent(source), ensure_ascii=False)


def note_has_attribution(attr_block: str) -> bool:
    for line in attr_block.splitlines():
        if line[:1].isspace() and re.search(r"\d", line):
            return True
    return False


def count_attributed_lines(attr_block: str) -> int:
    """Count the line numbers listed in the attribution block."""
    total = 0
    for line in attr_block.splitlines():
        if not line[:1].isspace():
            continue
        match = re.search(r"(\S+)\s+([\d,\- ]+)\s*$", line)
        if not match:
            continue
        for part in match.group(2).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                left, _, right = part.partition("-")
                try:
                    total += max(0, int(right) - int(left) + 1)
                except ValueError:
                    continue
            else:
                try:
                    int(part)
                    total += 1
                except ValueError:
                    continue
    return total


def note_sessions(text: str) -> list[dict]:
    parts = split_note(text)
    if not parts:
        return []
    try:
        obj = json.loads(parts[1])
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    sessions = obj.get("sessions")
    if not isinstance(sessions, dict):
        return []
    return [s for s in sessions.values() if isinstance(s, dict)]


def note_tools(text: str) -> list[str]:
    tools: list[str] = []
    for session in note_sessions(text):
        agent = session.get("agent_id")
        if isinstance(agent, dict):
            tool = agent.get("tool")
            model = agent.get("model")
            if tool:
                label = f"{tool}::{model}" if model else str(tool)
                if label not in tools:
                    tools.append(label)
    return tools


_NOTES_TREE_CACHE: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}


def invalidate_notes_cache(repo: object) -> None:
    prefix = norm_key(repo)
    for key in [k for k in _NOTES_TREE_CACHE if k[0] == prefix]:
        _NOTES_TREE_CACHE.pop(key, None)


def notes_tree(repo: object, ref: str = NOTES_REFNAME, *, refresh: bool = False) -> dict[str, tuple[str, str]]:
    """sha -> (note blob, tree path) read straight from a notes tree.

    git picks the tree fanout itself (flat, 1 or 2 levels, depending on how
    many notes exist), so the path must always come from ls-tree.
    """
    key = (norm_key(repo), ref)
    if not refresh and key in _NOTES_TREE_CACHE:
        return _NOTES_TREE_CACHE[key]
    result = git(["ls-tree", "-r", ref], repo)
    tree: dict[str, tuple[str, str]] = {}
    if result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            if "\t" not in line:
                continue
            meta, _, path = line.partition("\t")
            fields = meta.split()
            if len(fields) < 3:
                continue
            path = path.strip()
            sha = path.replace("/", "")
            if len(sha) == 40:
                tree[sha] = (fields[2], path)
    _NOTES_TREE_CACHE[key] = tree
    return tree


def notes_index(repo: object, ref: str = NOTES_REFNAME) -> dict[str, str]:
    """sha -> note blob (content identity, used for sync comparisons)."""
    return {sha: blob for sha, (blob, _path) in notes_tree(repo, ref).items()}


def read_note(repo: object, sha: str, ref: str = NOTES_REFNAME, *, refresh: bool = False) -> str | None:
    """Note text for one object, or None.

    The notes tree is cached per process; pass refresh=True while waiting for
    another process (the git-ai daemon) to publish a note.
    """
    entry = notes_tree(repo, ref, refresh=refresh).get(sha)
    if entry is None:
        # Not in the tree yet: let git itself resolve it for the standard ref.
        if not ref.startswith("refs/notes/"):
            return None
        result = git(["notes", f"--ref={ref}", "show", sha], repo)
        return result.stdout if result.returncode == 0 else None
    result = git(["cat-file", "-p", f"{ref}:{entry[1]}"], repo)
    return result.stdout if result.returncode == 0 else None


def write_note(repo: object, sha: str, text: str, ref: str = NOTES_REFNAME) -> bool:
    """Write a note verbatim: LF endings, UTF-8, no BOM."""
    handle = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "wb", suffix=".txt", delete=False, dir=str(home_dir()) if home_dir().is_dir() else None
        )
        handle.write(text.encode("utf-8"))
        handle.close()
        result = git(["notes", f"--ref={ref}", "add", "-f", "-F", handle.name, sha], repo)
        if result.returncode == 0:
            invalidate_notes_cache(repo)
            return True
        return False
    except OSError:
        return False
    finally:
        if handle is not None:
            try:
                os.unlink(handle.name)
            except OSError:
                pass


def grep_notes(repo: object, needle: str, ref: str = NOTES_REFNAME) -> list[str]:
    """Commit shas whose note blob contains needle (fast pre-filter)."""
    result = git(["grep", "-l", "-F", needle, ref], repo)
    if result.returncode != 0:
        return []
    shas: list[str] = []
    for line in (result.stdout or "").splitlines():
        if ":" not in line:
            continue
        _, _, path = line.partition(":")
        sha = path.replace("/", "").strip()
        if len(sha) == 40 and sha not in shas:
            shas.append(sha)
    return shas


def local_emails(repo: object, sha: str) -> list[str]:
    emails: list[str] = []
    configured = git_line(["config", "user.email"], repo)
    if configured:
        emails.append(configured)
    for fmt in ("%ae", "%ce"):
        value = git_line(["show", "-s", f"--format={fmt}", sha], repo)
        if value:
            emails.append(value)
    unique: list[str] = []
    for email in emails:
        if email and email not in unique:
            unique.append(email)
    return unique


def rewrite_mock_ai(text: str, tool: str, model: str) -> str | None:
    """Retarget a mock_ai checkpoint at the configured tool/model."""
    if not re.search(r'"tool"\s*:\s*"mock_ai"', text):
        return None
    parts = split_note(text)
    if parts:
        attr, json_text = parts
        try:
            obj = json.loads(json_text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            sessions = obj.get("sessions")
            changed = False
            if isinstance(sessions, dict):
                for session in sessions.values():
                    if not isinstance(session, dict):
                        continue
                    agent = session.get("agent_id")
                    if isinstance(agent, dict) and agent.get("tool") == "mock_ai":
                        agent["tool"] = tool
                        if model:
                            agent["model"] = model
                        changed = True
            if changed:
                return attr.rstrip("\n") + "\n---\n" + dump_json_like(obj, json_text) + "\n"
    # Fallback: surgical rewrite that keeps every other byte untouched.
    new_text = re.sub(r'("tool"\s*:\s*)"mock_ai"', lambda m: m.group(1) + json.dumps(tool), text)
    if model and model != DEFAULT_WHEN_UNKNOWN:
        def fix_model(match: re.Match) -> str:
            tail = text[match.end(): match.end() + 400]
            closing = tail.find("}")
            if closing < 0:
                return match.group(0)
            segment = tail[:closing]
            fixed = re.sub(r'("model"\s*:\s*)"[^"]*"', lambda m: m.group(1) + json.dumps(model), segment, count=1)
            if fixed == segment:
                return match.group(0)
            return match.group(0) + tail[:closing].replace(segment, fixed, 1)

        new_text = re.sub(r'"tool"\s*:\s*"mock_ai"', fix_model, new_text, count=1)
    return new_text if new_text != text else None


def convert_human_note(text: str, *, tool: str, model: str) -> str | None:
    """Turn human attributions into a real AI session, keeping existing ones."""
    parts = split_note(text)
    if not parts:
        return None
    attr, json_text = parts
    try:
        obj = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    humans = obj.get("humans")
    if not isinstance(humans, dict) or not humans:
        return None
    human_ids = list(dict.fromkeys(re.findall(r"h_[0-9a-f]+", attr)))
    if not human_ids:
        return None
    session_id = "s_" + uuid.uuid4().hex[:14]
    trace_id = "t_" + uuid.uuid4().hex[:14]
    author_id = f"{session_id}::{trace_id}"
    new_attr = attr
    for human_id in human_ids:
        new_attr = new_attr.replace(human_id, author_id)
    human_author = ""
    first = next((v for v in humans.values() if isinstance(v, dict)), None)
    if first:
        human_author = str(first.get("author") or "")
    sessions: dict = {}
    existing = obj.get("sessions")
    if isinstance(existing, dict):
        sessions.update(existing)
    sessions[session_id] = {
        "agent_id": {
            "tool": tool,
            "id": f"ai-thread-{int(time.time() * 1000)}",
            "model": model,
        },
        "human_author": human_author,
    }
    new_obj = {k: v for k, v in obj.items() if k not in ("humans", "sessions")}
    new_obj["sessions"] = sessions
    return new_attr.rstrip("\n") + "\n---\n" + dump_json_like(new_obj, json_text) + "\n"


def plan_note_change(
    repo: object,
    sha: str,
    text: str,
    *,
    tool: str,
    model: str,
    allow_mock: bool = True,
    allow_human: bool = False,
    emails: list[str] | None = None,
    subject_pattern: str | None = None,
) -> tuple[str | None, str]:
    """Return (new note text or None, kind)."""
    if allow_mock and re.search(r'"tool"\s*:\s*"mock_ai"', text):
        rewritten = rewrite_mock_ai(text, tool, model)
        return (rewritten, "mock_ai") if rewritten else (None, "unchanged")
    if not allow_human or '"humans"' not in text:
        return None, "none"
    if emails is None:
        emails = local_emails(repo, sha)
    if emails and not any(email in text for email in emails):
        return None, "foreign-human"
    if subject_pattern:
        subject = git_line(["show", "-s", "--format=%s", sha], repo, default="") or ""
        if not re.search(subject_pattern, subject):
            return None, "subject-mismatch"
    converted = convert_human_note(text, tool=tool, model=model)
    return (converted, "human") if converted else (None, "unchanged")


# ---------------------------------------------------------------- hooks
def staged_files(repo: object) -> list[str]:
    result = git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], repo)
    if result.returncode != 0:
        return []
    return [name for name in (result.stdout or "").split("\0") if name]


def wait_for_checkpoint(repo: object, seconds: float = 10.0) -> None:
    """Let the daemon persist the checkpoint before the commit moves on.

    The daemon archives working logs as old-<sha> once it has consumed them,
    so either location proves that the checkpoint was picked up.
    """
    head = git_line(["rev-parse", "HEAD"], repo)
    git_dir = git_line(["rev-parse", "--absolute-git-dir"], repo)
    if not git_dir:
        return
    base = Path(git_dir) / "ai" / "working_logs"
    key = head or "initial"
    candidates = [base / key / "checkpoints.jsonl", base / f"old-{key}" / "checkpoints.jsonl"]
    deadline = time.time() + (seconds if head else min(seconds, 3.0))
    while time.time() < deadline and not any(path.exists() for path in candidates):
        time.sleep(0.15)
    if any(path.exists() for path in candidates):
        time.sleep(0.3)


def cmd_hook_pre_commit(args: argparse.Namespace) -> int:
    """Never block a commit."""
    try:
        if os.environ.get(ENV_AUTO) == "0":
            return 0
        repo = current_repo()
        if repo is None:
            return 0
        executable = find_git_ai()
        if not executable:
            warn("git-ai not found; skipping AI attribution.")
            return 0
        files = staged_files(repo)
        if not files:
            return 0
        info("[git-ai] marking staged changes as AI code ...")
        result = run([executable, "checkpoint", "mock_ai", *files], cwd=repo, timeout=300)
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.returncode != 0:
            if result.stderr:
                sys.stderr.write(result.stderr)
            warn(
                f"git-ai checkpoint failed (exit={result.returncode}); "
                "commit continues without AI attribution."
            )
            return 0
        wait_for_checkpoint(repo)
    except Exception as exc:  # pragma: no cover - defensive
        warn(f"pre-commit adapter error: {exc!r}")
    return 0


def normalize_commit_note(repo: object, sha: str, config: dict, *, wait_seconds: float = 0.0) -> str:
    """Bring one commit's note to the configured tool/model.

    git-ai's own post_notes_updated hook cannot be relied on: in 1.7.x it is
    fired from the "git core hooks" path that upstream has sunset, so it only
    runs when git-ai's own hooks are installed through core.hooksPath.

    A note for the commit being created is published by the daemon only after
    the git process exits, so a commit hook cannot normalise its own commit -
    it normalises the previous one instead, which is always available.
    """
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    tool, model = resolve_tool_model(config, repo)
    convert_human = bool(defaults.get("convertLocalHuman", True))
    deadline = time.time() + max(0.0, wait_seconds)
    note = read_note(repo, sha, refresh=True)
    while note is None and time.time() < deadline:
        time.sleep(0.3)
        note = read_note(repo, sha, refresh=True)
    if note is None:
        return "no-note"
    new_text, kind = plan_note_change(
        repo,
        sha,
        note,
        tool=tool,
        model=model,
        allow_mock=True,
        allow_human=convert_human,
    )
    if kind == "foreign-human":
        clear_repo_map(sha)
        return kind
    if not new_text:
        return kind
    if not new_text.endswith("\n"):
        new_text += "\n"
    if not write_note(repo, sha, new_text):
        log_event("notes-updated", repo=repo, commit=sha, tool=tool, model=model,
                  status="write-failed", detail=kind)
        return "write-failed"
    clear_repo_map(sha)
    log_event("notes-updated", repo=repo, commit=sha, tool=tool, model=model, detail=kind)
    return f"rewritten:{kind}"


def normalize_repo_notes(repo: object, config: dict, *, allow_human: bool | None = None,
                         limit: int | None = None, apply: bool = True) -> dict[str, int]:
    """One normalization pass over every note of a repository."""
    tool, model = resolve_tool_model(config, repo)
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    if allow_human is None:
        allow_human = bool(defaults.get("convertLocalHuman", True))
    return scan_and_rewrite(
        repo,
        tool=tool,
        model=model,
        allow_mock=True,
        allow_human=allow_human,
        emails=None,
        subject_pattern=None,
        apply=apply,
        limit=limit,
    )


def spawn_detached(args: list[str]) -> bool:
    """Start a helper that outlives this process (commit hooks must never wait)."""
    if is_frozen():
        cmd = [str(manager_script())] + args
    else:
        cmd = [find_python(), str(manager_script()), "--home", str(home_dir())] + args
    try:
        if os.name == "nt":
            flags = 0
            for name in ("DETACHED_PROCESS", "CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
                flags |= getattr(subprocess, name, 0)
            subprocess.Popen(
                cmd, creationflags=flags, close_fds=True, cwd=str(app_dir()),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                cmd, start_new_session=True, close_fds=True, cwd=str(app_dir()),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return True
    except OSError as exc:
        warn(f"无法启动后台归一化助手：{exc}")
        return False


def cmd_hook_normalize_later(args: argparse.Namespace) -> int:
    """后台助手：等 daemon 写完 note，再把它归一化成配置的工具/模型。

    post-commit 钩子在 git 进程内，永远看不到自己这条提交的 note（daemon 要等
    git 退出后才发布，实测约 0.6 秒），所以钩子把这件事交给这个后台进程，
    提交本身完全不被阻塞。
    """
    repo = expand(args.repo) if getattr(args, "repo", None) else current_repo()
    sha = str(getattr(args, "commit", "") or "").strip()
    if not repo or not sha:
        return 0
    try:
        config = load_config()
    except SystemExit:
        config = default_config()
    try:
        wait = float(getattr(args, "wait", None) or 30)
    except (TypeError, ValueError):
        wait = 30.0
    deadline = time.time() + max(1.0, wait)
    while time.time() < deadline:
        note = read_note(repo, sha, refresh=True)
        if note is not None:
            result = normalize_commit_note(repo, sha, config)
            if not result.startswith("rewritten"):
                log_event("normalize-later", repo=repo, commit=sha, status=result)
            return 0
        time.sleep(0.4)
    log_event("normalize-later", repo=repo, commit=sha, status="note-timeout")
    return 0


def cmd_hook_post_commit(args: argparse.Namespace) -> int:
    """Record the repo mapping, normalise the previous commit, and hand the new
    one to a background helper (its note does not exist yet)."""
    try:
        head = git_line(["rev-parse", "HEAD"])
        repo = git_line(["rev-parse", "--show-toplevel"])
        if not (head and repo):
            return 0
        write_repo_map(head, repo)
        log_event("post-commit", repo=repo, commit=head)
        config: dict
        try:
            config = load_config()
        except SystemExit:
            config = default_config()
        defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
        if not defaults.get("normalizeOnCommit", True):
            return 0
        results: list[str] = []
        parent = git_line(["rev-parse", "--verify", "--quiet", "HEAD^"])
        if parent:
            results.append(normalize_commit_note(repo, parent, config))
        # 万一已经有了（amend / 重新提交），顺手处理
        own = normalize_commit_note(repo, head, config)
        if own != "no-note":
            results.append(f"HEAD:{own}")
        else:
            try:
                wait = float(defaults.get("noteWaitSeconds", 30))
            except (TypeError, ValueError):
                wait = 30.0
            spawn_detached([
                "hook", "normalize-later",
                "--repo", str(repo), "--commit", head, "--wait", str(int(wait)),
            ])
        changed = [r for r in results if r.startswith("rewritten")]
        if changed:
            info(f"[git-ai] note normalized ({', '.join(changed)})")
    except Exception as exc:  # pragma: no cover - defensive
        warn(f"post-commit adapter error: {exc!r}")
    return 0


def write_repo_map(sha: str, repo: object) -> None:
    directory = repo_map_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / sha
    tmp = directory / f"{sha}.tmp"
    tmp.write_bytes(str(Path(str(repo))).encode("utf-8"))
    os.replace(tmp, target)


def mapped_repo(sha: str) -> Path | None:
    path = repo_map_dir() / sha
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if is_repo(candidate) else None


def clear_repo_map(sha: str) -> None:
    try:
        (repo_map_dir() / sha).unlink()
    except OSError:
        pass


def prune_repo_map(max_age_days: float) -> int:
    directory = repo_map_dir()
    if not directory.is_dir():
        return 0
    if max_age_days <= 0:
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def parse_hook_events(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def cmd_notes_updated(args: argparse.Namespace) -> int:
    """git-ai post_notes_updated hook: normalise freshly written notes."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return 0
        raw = sys.stdin.read()
    except Exception:
        return 0
    events = parse_hook_events(raw)
    if not events:
        return 0
    try:
        config = load_config()
    except SystemExit:
        config = default_config()
    for event in events:
        try:
            sha = str(event.get("commit_sha") or event.get("sha") or "").strip()
            if not sha:
                continue
            repo = mapped_repo(sha)
            if repo is None:
                continue
            tool, model = resolve_tool_model(config, repo)
            defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
            convert_human = bool(defaults.get("convertLocalHuman", True))
            note = read_note(repo, sha)
            if note is None:
                continue
            new_text, kind = plan_note_change(
                repo,
                sha,
                note,
                tool=tool,
                model=model,
                allow_mock=True,
                allow_human=convert_human,
            )
            if not new_text:
                if kind == "foreign-human":
                    clear_repo_map(sha)
                continue
            if not new_text.endswith("\n"):
                new_text += "\n"
            if write_note(repo, sha, new_text):
                log_event(
                    "notes-updated",
                    repo=repo,
                    commit=sha,
                    tool=tool,
                    model=model,
                    status="ok",
                    detail=kind,
                )
                clear_repo_map(sha)
            else:
                log_event(
                    "notes-updated",
                    repo=repo,
                    commit=sha,
                    tool=tool,
                    model=model,
                    status="write-failed",
                    detail=kind,
                )
        except Exception as exc:  # pragma: no cover - defensive
            warn(f"notes-updated: {exc!r}")
    return 0


# ---------------------------------------------------------------- hook install
def hook_body(kind: str, python: str, manager: str) -> str:
    """Shell body of a managed hook.

    A frozen (exe) build runs itself, so the target machine needs no Python at
    all; a source build calls the configured interpreter plus the script.
    """
    if is_frozen():
        command = f'"{abs_posix(manager)}" hook {kind}'
    else:
        command = f'"{abs_posix(python)}" "{abs_posix(manager)}" hook {kind}'
    return "\n".join(
        [
            BLOCK_BEGIN,
            f"# Managed by {APP} - regenerated by `{APP} install`. Edit outside this block.",
            f"{command} || true",
            BLOCK_END,
            "",
        ]
    )


def legacy_body(kind: str) -> str:
    name = "precommit-ai.ps1" if kind == "pre-commit" else "postcommit-ai.ps1"
    script = home_dir() / name
    return "\n".join(
        [
            "#!/bin/sh",
            f"# git-ai auto-attribution adapter ({name}).",
            f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File '{abs_posix(script)}' || true",
            "",
        ]
    )


def strip_legacy(text: str) -> str:
    """Drop the PowerShell adapter lines and any wrapper left empty behind them."""
    kept = [line for line in text.splitlines() if not any(m in line for m in LEGACY_MARKERS)]
    cleaned = "\n".join(kept)
    wrapper = (
        r"(?m)^[ \t]*#[ \t]*BEGIN git-ai note normalizer[ \t]*\n"
        r"(?:[ \t]*\n)*"
        r"^[ \t]*#[ \t]*END git-ai note normalizer[ \t]*\n?"
    )
    return re.sub(wrapper, "", cleaned)


def render_hook(kind: str, python: str, manager: str, existing: str | None,
                *, legacy: bool = False) -> str:
    block = legacy_body(kind) if legacy else hook_body(kind, python, manager)
    if existing and BLOCK_BEGIN in existing and BLOCK_END in existing:
        pattern = re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"[ \t]*\n?"
        replaced = re.sub(pattern, block.rstrip("\n") + "\n", existing, flags=re.S)
        if not replaced.endswith("\n"):
            replaced += "\n"
        return replaced
    text = strip_legacy(existing or "").strip("\n")
    if not text:
        text = "#!/bin/sh"
    elif not text.startswith("#!"):
        text = "#!/bin/sh\n" + text
    return text.rstrip("\n") + "\n\n" + block


def write_hook(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    try:
        path.chmod(0o755)
    except OSError:
        pass


def configure_git_ai_hook(python: str, manager: str, *, dry_run: bool, restart: bool) -> bool:
    executable = find_git_ai()
    if not executable:
        warn(f"git-ai not found; set GIT_AI_EXE or install it. Cannot configure {GIT_AI_HOOK_KEY}.")
        return False
    command = (f'"{abs_posix(manager)}" notes-updated' if is_frozen()
               else f'"{abs_posix(python)}" "{abs_posix(manager)}" notes-updated')
    if dry_run:
        info(f"  would set {GIT_AI_HOOK_KEY} = {command}")
        return True
    git_argvs = [
        [executable, "config", "unset", GIT_AI_HOOK_KEY],
        [executable, "config", "--add", GIT_AI_HOOK_KEY, command],
    ]
    for argv in git_argvs:
        run(argv)
    check = run([executable, "config", GIT_AI_HOOK_KEY])
    configured = check.returncode == 0 and "notes-updated" in (check.stdout or "")
    if restart and configured:
        run([executable, "bg", "restart", "--hard"], timeout=120)
    return configured


def emit_json(payload: object) -> int:
    """Machine readable output for scripts and agents."""
    info(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    python = find_python(getattr(args, "python", None))
    manager = manager_script()
    installed: list[str] = []
    unchanged: list[str] = []
    failed: list[str] = []
    for repo in repos:
        for kind in HOOK_KINDS:
            path = repo / ".git" / "hooks" / kind
            try:
                existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
            except OSError:
                existing = None
            new_text = render_hook(kind, python, manager, existing, legacy=bool(getattr(args, "legacy", False)))
            label = f"{repo.name}/{kind}"
            if existing is not None and existing == new_text:
                unchanged.append(label)
                continue
            if getattr(args, "dry_run", False):
                installed.append(label + " (dry-run)")
                continue
            try:
                write_hook(path, new_text)
                installed.append(label)
            except OSError as exc:
                failed.append(f"{label} ({exc})")
    pruned = 0 if getattr(args, "dry_run", False) else prune_repo_map(30)
    hook_ok = None
    if not getattr(args, "no_git_ai_config", False):
        hook_ok = configure_git_ai_hook(
            python,
            manager,
            dry_run=bool(getattr(args, "dry_run", False)),
            restart=not bool(getattr(args, "no_daemon_restart", False)),
        )
    payload = {
        "python": python,
        "manager": str(manager),
        "home": str(home_dir()),
        "git_ai": find_git_ai(),
        "git_ai_hook_configured": hook_ok,
        "installed": installed,
        "unchanged": unchanged,
        "failed": failed,
        "pruned_map_entries": pruned,
        "dry_run": bool(getattr(args, "dry_run", False)),
    }
    if getattr(args, "json", False):
        return emit_json(payload)
    info("=== git-ai manager install ===")
    if is_frozen():
        info("运行方式      : 独立 exe（目标机器不需要 Python）")
    else:
        info(f"python        : {python}")
    info(f"manager       : {manager}")
    info(f"git-ai home   : {home_dir()}")
    info(f"git-ai binary : {find_git_ai() or '(not found)'}")
    if hook_ok is not None:
        info(f"git-ai hook   : {'configured' if hook_ok else 'NOT configured'}")
    info("")
    info(f"Installed/updated ({len(installed)}):")
    for label in installed:
        info(f"  + {label}")
    info(f"Already current ({len(unchanged)}):")
    for label in unchanged:
        info(f"  = {label}")
    if failed:
        info(f"Failed ({len(failed)}):")
        for label in failed:
            info(f"  ! {label}")
    if pruned:
        info(f"Pruned {pruned} stale entries from {repo_map_dir()}")
    info("")
    info("Done.")
    return 1 if failed else 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    restore_legacy = bool(getattr(args, "restore_legacy", False))
    python = find_python(getattr(args, "python", None))
    manager = manager_script()
    changed: list[str] = []
    for repo in repos:
        for kind in HOOK_KINDS:
            path = repo / ".git" / "hooks" / kind
            if not path.exists():
                continue
            try:
                existing = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if BLOCK_BEGIN not in existing and not any(m in existing for m in LEGACY_MARKERS):
                continue
            label = f"{repo.name}/{kind}"
            if getattr(args, "dry_run", False):
                changed.append(label + " (dry-run)")
                continue
            if restore_legacy:
                write_hook(path, render_hook(kind, python, manager, existing, legacy=True))
                changed.append(label + " (legacy powershell restored)")
                continue
            cleaned = strip_legacy(existing)
            cleaned = re.sub(
                re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"[ \t]*\n?",
                "",
                cleaned,
                flags=re.S,
            ).strip("\n")
            meaningful = [
                ln for ln in cleaned.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
            ]
            try:
                if not meaningful:
                    path.unlink()
                    changed.append(label + " (removed)")
                else:
                    write_hook(path, cleaned + "\n")
                    changed.append(label + " (block removed)")
            except OSError as exc:
                changed.append(f"{label} ({exc})")
    if not getattr(args, "keep_git_ai_config", False):
        executable = find_git_ai()
        if executable and not getattr(args, "dry_run", False):
            run([executable, "config", "unset", GIT_AI_HOOK_KEY])
            run([executable, "bg", "restart", "--hard"], timeout=120)
    if getattr(args, "json", False):
        return emit_json({
            "home": str(home_dir()),
            "changed": changed,
            "git_ai_hook_cleared": not bool(getattr(args, "keep_git_ai_config", False)),
            "dry_run": bool(getattr(args, "dry_run", False)),
        })
    info("=== git-ai manager uninstall ===")
    for label in changed:
        info(f"  - {label}")
    if not changed:
        info("  nothing to do")
    return 0


# ---------------------------------------------------------------- sync notes
def rev_parse(repo: object, ref: str) -> str | None:
    return git_line(["rev-parse", "--verify", "--quiet", ref], repo)


def refresh_remote_snapshot(repo: object, remote: str) -> tuple[str | None, str | None]:
    """Fetch refs/notes/ai into a private ref.  Returns (sha, error)."""
    git(["update-ref", "-d", REMOTE_SNAPSHOT_REF], repo)
    result = git(
        ["fetch", "--quiet", "--no-tags", "--force", remote, f"+{NOTES_REFNAME}:{REMOTE_SNAPSHOT_REF}"],
        repo,
        timeout=600,
    )
    if result.returncode == 0:
        return rev_parse(repo, REMOTE_SNAPSHOT_REF), None
    stderr = (result.stderr or "").lower()
    if "couldn't find remote ref" in stderr or "could not find remote ref" in stderr:
        return None, None
    return None, (result.stderr or result.stdout or "fetch failed").strip().splitlines()[-1]


def sync_repo(repo: object, *, remote: str, dry_run: bool, push: bool) -> tuple[str, str]:
    local = rev_parse(repo, NOTES_REFNAME)
    snapshot, error = refresh_remote_snapshot(repo, remote)
    if error:
        return "fetch-failed", error
    if local and snapshot and local == snapshot:
        return "in-sync", ""
    if not local and not snapshot:
        return "no-notes", ""
    if local and not snapshot:
        if not push:
            return "push-skipped", "remote has no notes ref"
        if dry_run:
            return "would-push", "remote has no notes ref"
        result = git(["push", "--quiet", remote, f"{NOTES_REFNAME}:{NOTES_REFNAME}"], repo, timeout=600)
        if result.returncode == 0:
            mark_snapshot_current(repo)
            return "pushed", ""
        return "push-failed", tail_error(result)
    if snapshot and not local:
        if dry_run:
            return "would-adopt-remote", ""
        git(["update-ref", NOTES_REFNAME, snapshot], repo)
        return "adopted-remote", ""
    if dry_run:
        return "would-merge", "local and remote diverge"
    merge = git(
        ["notes", f"--ref={NOTES_REFNAME}", "merge", "-s", "ours", REMOTE_SNAPSHOT_REF],
        repo,
        timeout=600,
    )
    if merge.returncode != 0:
        return "merge-failed", tail_error(merge)
    invalidate_notes_cache(repo)
    if not push:
        return "merged", "not pushed (--no-push)"
    result = git(["push", "--quiet", remote, f"{NOTES_REFNAME}:{NOTES_REFNAME}"], repo, timeout=600)
    if result.returncode == 0:
        mark_snapshot_current(repo)
        return "merged-pushed", ""
    return "push-failed", tail_error(result)


def mark_snapshot_current(repo: object) -> None:
    """Remember that the remote now holds exactly what we have.

    Without this the remote column of `status` keeps saying "pending" after a
    successful push, because the snapshot still reflects the last fetch.
    """
    local = rev_parse(repo, NOTES_REFNAME)
    if local:
        git(["update-ref", REMOTE_SNAPSHOT_REF, local], repo)
        invalidate_notes_cache(repo)


def tail_error(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout or "").strip()
    if not text:
        return f"exit={result.returncode}"
    return text.splitlines()[-1]


def cmd_sync_notes(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    remote = resolve_remote(config, getattr(args, "remote", None))
    dry_run = bool(getattr(args, "dry_run", False))
    do_normalize = getattr(args, "normalize", True)
    rows: list[list[object]] = []
    failures = 0
    rewritten_total = 0
    payload: list[dict] = []
    for repo in repos:
        normalized = 0
        if do_normalize:
            counts = normalize_repo_notes(
                repo,
                config,
                allow_human=None,
                limit=getattr(args, "normalize_limit", None),
                apply=not dry_run,
            )
            normalized = counts["mock_ai"] + counts["human"]
            rewritten_total += normalized
        status, detail = sync_repo(
            repo,
            remote=remote,
            dry_run=dry_run,
            push=not bool(getattr(args, "no_push", False)),
        )
        rows.append([repo.name, status, normalized or "", trunc(detail, 60)])
        payload.append(
            {
                "repo": str(repo),
                "name": repo.name,
                "result": status,
                "normalized": normalized,
                "detail": detail,
            }
        )
        if status.endswith("failed"):
            failures += 1
        log_event("sync-notes", repo=repo, status=status, detail=detail or None, count=normalized or None)
    if getattr(args, "json", False):
        info(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1 if failures else 0
    info(f"=== git-ai notes sync (remote: {remote}) ===")
    info(render_table(["REPOSITORY", "RESULT", "NORMALIZED", "DETAIL"], rows) if rows else "no repositories")
    if do_normalize:
        info(f"normalized {rewritten_total} note(s) before syncing")
    if dry_run:
        info("(dry run - nothing was written or pushed)")
    return 1 if failures else 0


# ---------------------------------------------------------------- bulk rewrites
def scan_and_rewrite(
    repo: object,
    *,
    tool: str,
    model: str,
    allow_mock: bool,
    allow_human: bool,
    emails: list[str] | None,
    subject_pattern: str | None,
    apply: bool,
    limit: int | None = None,
) -> dict[str, int]:
    """Rewrite every note of one repository that matches the rules."""
    counts = {"scanned": 0, "mock_ai": 0, "human": 0, "foreign-human": 0, "failed": 0, "unchanged": 0}
    candidates: list[str] = []
    if allow_mock:
        candidates += grep_notes(repo, '"tool": "mock_ai"', NOTES_REFNAME)
        candidates += grep_notes(repo, '"tool":"mock_ai"', NOTES_REFNAME)
    if allow_human:
        for email in (emails or [""]):
            candidates += grep_notes(repo, email if email else '"humans"', NOTES_REFNAME)
    unique: list[str] = list(dict.fromkeys(candidates))
    if not unique and allow_human:
        unique = list(notes_index(repo, NOTES_REFNAME).keys())
    for sha in unique:
        if limit is not None and counts["scanned"] >= limit:
            break
        note = read_note(repo, sha)
        if note is None:
            continue
        counts["scanned"] += 1
        new_text, kind = plan_note_change(
            repo,
            sha,
            note,
            tool=tool,
            model=model,
            allow_mock=allow_mock,
            allow_human=allow_human,
            emails=emails,
            subject_pattern=subject_pattern,
        )
        if kind in counts:
            counts[kind] += 1
        if not new_text:
            continue
        if not apply:
            continue
        if not new_text.endswith("\n"):
            new_text += "\n"
        if write_note(repo, sha, new_text):
            log_event("rewrite", repo=repo, commit=sha, tool=tool, model=model, detail=kind)
        else:
            counts["failed"] += 1
            warn(f"{Path(str(repo)).name}: failed to write note for {sha}")
    return counts


def cmd_normalize(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    apply = bool(getattr(args, "apply", False))
    include_human = bool(getattr(args, "include_human", False))
    subject = getattr(args, "subject_pattern", None)
    rows: list[list[object]] = []
    payload: list[dict] = []
    total = 0
    for repo in repos:
        tool, model = resolve_tool_model(config, repo)
        emails = list(getattr(args, "email", None) or []) or None
        counts = scan_and_rewrite(
            repo,
            tool=tool,
            model=model,
            allow_mock=True,
            allow_human=include_human,
            emails=emails,
            subject_pattern=subject,
            apply=apply,
            limit=getattr(args, "limit", None),
        )
        matched = counts["mock_ai"] + counts["human"]
        total += matched
        if counts["scanned"] or matched:
            rows.append(
                [
                    repo.name,
                    counts["mock_ai"],
                    counts["human"],
                    counts["foreign-human"],
                    counts["failed"],
                    f"{tool}::{model}",
                ]
            )
            payload.append(
                {
                    "repo": str(repo),
                    "name": repo.name,
                    "scanned": counts["scanned"],
                    "mock_ai": counts["mock_ai"],
                    "human": counts["human"],
                    "foreign_human": counts["foreign-human"],
                    "failed": counts["failed"],
                    "tool": tool,
                    "model": model,
                }
            )
    if getattr(args, "json", False):
        return emit_json({
            "mode": "apply" if apply else "dry-run",
            "include_human": include_human,
            "total_rewritable": total,
            "repos": payload,
        })
    info("=== git-ai note normalization ===")
    info(render_table(["REPOSITORY", "MOCK_AI", "HUMAN", "OTHER-HUMAN", "FAILED", "TOOL::MODEL"], rows)
         if rows else "nothing matched")
    info(f"total rewritable notes: {total}")
    info("mode: APPLY" if apply else "mode: DRY-RUN (re-run with --apply to write)")
    return 0


def cmd_convert_human(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    apply = bool(getattr(args, "apply", False))
    emails = list(getattr(args, "email", None) or []) or None
    subject = getattr(args, "subject_pattern", None)
    rows: list[list[object]] = []
    total = 0
    for repo in repos:
        tool, model = resolve_tool_model(config, repo)
        counts = scan_and_rewrite(
            repo,
            tool=tool,
            model=model,
            allow_mock=False,
            allow_human=True,
            emails=emails,
            subject_pattern=subject,
            apply=apply,
            limit=getattr(args, "limit", None),
        )
        total += counts["human"]
        if counts["human"] or counts["scanned"]:
            rows.append([repo.name, counts["scanned"], counts["human"], counts["failed"], f"{tool}::{model}"])
    info("=== git-ai human note conversion ===")
    info(f"email filter    : {', '.join(emails) if emails else '(local git users)'}")
    info(f"subject pattern : {subject or '(none)'}")
    info(render_table(["REPOSITORY", "SCANNED", "CONVERTED", "FAILED", "TOOL::MODEL"], rows)
         if rows else "nothing matched")
    info(f"total converted: {total}")
    info("mode: APPLY" if apply else "mode: DRY-RUN (re-run with --apply to write)")
    return 0


def cmd_refresh_notes(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    apply = bool(getattr(args, "apply", False))
    include_human = getattr(args, "include_human", None)
    rows: list[list[object]] = []
    for repo in repos:
        tool, model = resolve_tool_model(config, repo)
        defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
        allow_human = bool(defaults.get("convertLocalHuman", True)) if include_human is None else bool(include_human)
        counts = scan_and_rewrite(
            repo,
            tool=tool,
            model=model,
            allow_mock=True,
            allow_human=allow_human,
            emails=list(getattr(args, "email", None) or []) or None,
            subject_pattern=None,
            apply=apply,
            limit=getattr(args, "limit", None),
        )
        rows.append(
            [
                repo.name,
                counts["scanned"],
                counts["mock_ai"],
                counts["human"],
                counts["failed"],
                f"{tool}::{model}",
            ]
        )
    info("=== git-ai refresh notes ===")
    info(render_table(["REPOSITORY", "SCANNED", "MOCK_AI", "HUMAN", "FAILED", "TOOL::MODEL"], rows))
    info("mode: APPLY" if apply else "mode: DRY-RUN (re-run with --apply to write)")
    return 0


# ---------------------------------------------------------------- repair empty
def added_lines_by_file(repo: object, sha: str, parents: int) -> dict[str, list[int]]:
    """Added line numbers per file, from a zero-context diff.

    Regular commits diff against their parent; root commits diff against the
    empty tree via diff-tree --root.
    """
    if parents == 1:
        argv = ["-c", "core.quotepath=false", "diff", "--unified=0", "--no-color", f"{sha}^", sha, "--"]
    else:
        argv = ["-c", "core.quotepath=false", "diff-tree", "--no-commit-id", "--root",
                "--unified=0", "-p", sha]
    result = git(argv, repo)
    if result.returncode != 0:
        return {}
    out: dict[str, list[int]] = {}
    current: str | None = None
    new_line = 0
    for line in (result.stdout or "").split("\n"):
        if line.startswith("+++ b/"):
            current = line[6:]
            out.setdefault(current, [])
            continue
        if line.startswith("+++ /dev/null"):
            current = None
            continue
        if line.startswith("@@"):
            match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                new_line = int(match.group(1))
            continue
        if current is None or new_line <= 0:
            continue
        if line.startswith("+"):
            if line.startswith("+++"):
                continue
            out[current].append(new_line)
            new_line += 1
            continue
        if line.startswith("-"):
            continue
        if line.startswith("\\"):
            continue
        new_line += 1
    return {path: lines for path, lines in out.items() if lines}


def lines_to_ranges(lines: list[int]) -> list[str]:
    ordered = sorted(set(lines))
    if not ordered:
        return []
    ranges: list[str] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = value
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ranges


def git_ai_version() -> str:
    executable = find_git_ai()
    if executable:
        result = run([executable, "version"], timeout=30)
        if result.returncode == 0:
            first = (result.stdout or "").strip().splitlines()
            if first:
                version = first[0].strip()
                if version.lower().startswith("git version"):
                    warn(
                        f"{executable} answered like a plain git passthrough, so the "
                        "installed git-ai build cannot report its own version."
                    )
                    return DEFAULT_WHEN_UNKNOWN
                return version
    return DEFAULT_WHEN_UNKNOWN


def build_repair_note(repo: object, sha: str, tool: str, model: str, version: str) -> str | None:
    parents_line = git_line(["rev-list", "--parents", "-n", "1", sha], repo)
    if not parents_line:
        return None
    parent_count = len(parents_line.split()) - 1
    if parent_count > 1:
        return None  # merge commit: no single meaningful diff
    added = added_lines_by_file(repo, sha, parent_count)
    if not added:
        return None
    session_id = "s_" + uuid.uuid4().hex[:14]
    trace_id = "t_" + uuid.uuid4().hex[:14]
    author_id = f"{session_id}::{trace_id}"
    human_author = git_line(["show", "-s", "--format=%an <%ae>", sha], repo, default="") or ""
    attr_lines: list[str] = []
    for path in sorted(added):
        ranges = lines_to_ranges(added[path])
        if not ranges:
            continue
        attr_lines.append(path)
        attr_lines.append(f"  {author_id} {','.join(ranges)}")
    if not attr_lines:
        return None
    meta = {
        "schema_version": "authorship/3.0.0",
        "git_ai_version": version,
        "base_commit_sha": sha,
        "prompts": {},
        "sessions": {
            session_id: {
                "agent_id": {
                    "tool": tool,
                    "id": f"ai-thread-{int(time.time() * 1000)}",
                    "model": model,
                },
                "human_author": human_author,
            }
        },
    }
    return "\n".join(attr_lines) + "\n---\n" + json.dumps(meta, ensure_ascii=False, indent=2) + "\n"


def empty_note_candidates(repo: object) -> list[str]:
    candidates: list[str] = []
    for sha in notes_index(repo, NOTES_REFNAME):
        note = read_note(repo, sha)
        if note is None:
            continue
        parts = split_note(note)
        attr = parts[0] if parts else note
        if not note_has_attribution(attr):
            candidates.append(sha)
    return candidates


def commits_missing_notes(
    repo: object,
    *,
    rev: str = "HEAD",
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[str]:
    """Commits reachable from rev that have no authorship note at all.

    These are the commits made before git-ai (or this tool) was in the picture:
    there is nothing to rewrite, the attribution has to be built from the diff.
    """
    argv = ["log", "--format=%H"]
    if since:
        argv.append(f"--since={since}")
    if until:
        argv.append(f"--until={until}")
    argv.append(rev)
    text = git_text(argv, repo)
    if text is None:
        return []
    known = notes_index(repo, NOTES_REFNAME)
    missing = [line.strip() for line in text.splitlines() if line.strip() and line.strip() not in known]
    if limit is not None and limit > 0:
        missing = missing[:limit]
    return missing


def count_commits(repo: object, *, rev: str = "HEAD", since: str | None = None,
                  until: str | None = None) -> int:
    argv = ["rev-list", "--count"]
    if since:
        argv.append(f"--since={since}")
    if until:
        argv.append(f"--until={until}")
    argv.append(rev)
    value = git_line(argv, repo)
    try:
        return int(value or 0)
    except ValueError:
        return 0


def cmd_repair_empty(args: argparse.Namespace) -> int:
    config = load_config()
    if getattr(args, "repo", None):
        repo = expand(args.repo)
        if not is_repo(repo):
            raise SystemExit(f"{APP}: not a git repository: {repo}")
    else:
        repo = current_repo()
        if repo is None:
            raise SystemExit(f"{APP}: pass --repo <path> (or run inside a repository)")
    tool, model = resolve_tool_model(config, repo)
    commits: list[str] = []
    for value in getattr(args, "commits", None) or []:
        commits += [c for c in re.split(r"[,\s]+", value) if c]
    if getattr(args, "scan", False):
        commits += empty_note_candidates(repo)
    rev = getattr(args, "branch", None) or "HEAD"
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    limit = getattr(args, "limit", None)
    span = (f"（{since or '最早'} ~ {until or '最新'}）" if (since or until) else "")
    scanned = 0
    missing_total = 0
    if getattr(args, "scan_missing", False):
        # 先做「廉价扫描」：只数数，不算 diff，几秒就能出结果
        scanned = count_commits(repo, rev=rev, since=since, until=until)
        found = commits_missing_notes(repo, rev=rev, since=since, until=until, limit=None)
        missing_total = len(found)
        if getattr(args, "count_only", False):
            info("=== git-ai 历史补全（仅统计）===")
            info(f"repository : {repo}")
            info(f"范围       : {rev}{span}")
            info(f"提交总数   : {scanned}")
            info(f"已有记录   : {scanned - missing_total}")
            info(f"缺记录     : {missing_total}")
            if missing_total:
                info("")
                info(f"补全命令   : {APP} repair-empty --repo \"{repo}\" --scan-missing --apply")
            return 0
        if limit is None:
            limit = DEFAULT_BACKFILL_LIMIT
        commits += found[:limit] if (limit and limit > 0) else found
    commits = list(dict.fromkeys(commits))
    if not commits:
        if getattr(args, "scan_missing", False):
            info(f"=== git-ai 历史补全 ===")
            info(f"repository : {repo}")
            info(f"范围       : {rev}{f'（since {since}）' if since else ''}")
            info(f"扫描 {scanned} 条提交，全部都有 AI 记录，无需补全。")
            return 0
        raise SystemExit(
            f"{APP}: nothing to repair - pass --commits <sha,...>, --scan (notes without line "
            f"attribution) or --scan-missing (commits without any note)"
        )
    version = git_ai_version()
    apply = bool(getattr(args, "apply", False))
    rows: list[list[object]] = []
    written = 0
    for sha in commits:
        full = git_line(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], repo)
        if not full:
            rows.append([trunc(sha, 12), "not-found", "", ""])
            continue
        note = read_note(repo, full)
        parts = split_note(note) if note else None
        if parts and note_has_attribution(parts[0]) and not getattr(args, "force", False):
            rows.append([full[:12], "has-attribution", "", "use --force to overwrite"])
            continue
        built = build_repair_note(repo, full, tool, model, version)
        if not built:
            rows.append([full[:12], "skipped", "", "merge commit or no added lines"])
            continue
        lines = count_attributed_lines(split_note(built)[0]) if split_note(built) else 0
        if apply:
            if write_note(repo, full, built):
                written += 1
                log_event("repair-empty", repo=repo, commit=full, tool=tool, model=model, count=lines)
                rows.append([full[:12], "written", lines, f"{tool}::{model}"])
            else:
                rows.append([full[:12], "write-failed", lines, ""])
        else:
            rows.append([full[:12], "would-write", lines, f"{tool}::{model}"])
            if getattr(args, "show", False):
                info("-" * 60)
                info(built)
    if getattr(args, "json", False):
        return emit_json({
            "repo": str(repo),
            "rev": rev,
            "since": since,
            "until": until,
            "scanned": scanned,
            "missing": missing_total,
            "processed": len(commits),
            "written": written,
            "mode": "apply" if apply else "dry-run",
            "tool": tool,
            "model": model,
            "rows": [
                {"commit": row[0], "result": row[1], "ai_lines": row[2], "note": row[3]}
                for row in rows
            ],
        })
    info("=== git-ai empty note repair ===")
    info(f"repository : {repo}")
    if getattr(args, "scan_missing", False):
        info(f"范围       : {rev}{f'（since {since}）' if since else ''}")
        info(f"扫描 {scanned} 条提交：已有记录 {scanned - missing_total} 条，"
             f"缺记录 {missing_total} 条，本次处理 {len(commits)} 条"
             + (f"（--limit {limit}）" if limit and missing_total > limit else ""))
    info(render_table(["COMMIT", "RESULT", "AI-LINES", "NOTE"], rows))
    if written:
        info(f"已写入 {written} 条 note（工具/模型：{tool}::{model}）")
    info("mode: APPLY" if apply else "mode: DRY-RUN (re-run with --apply to write)")
    return 0


# ---------------------------------------------------------------- reporting
def hook_state(repo: object) -> str:
    states: list[str] = []
    for kind in HOOK_KINDS:
        path = Path(str(repo)) / ".git" / "hooks" / kind
        if not path.exists():
            states.append("-")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            states.append("?")
            continue
        if BLOCK_BEGIN in text and "git-ai-manager" in text:
            states.append("mgr")
        elif any(m in text for m in LEGACY_MARKERS):
            states.append("ps1")
        else:
            states.append("other")
    return "/".join(states)


def remote_state(repo: object, index: dict[str, str], snapshot: dict[str, str]) -> str:
    if not index:
        return "no-local-notes"
    if not snapshot:
        return "no-remote-snapshot"
    missing = 0
    for sha, blob in index.items():
        if snapshot.get(sha) != blob:
            missing += 1
    extra = sum(1 for sha in snapshot if sha not in index)
    if missing == 0 and extra == 0:
        return "synced"
    return f"pending({missing})" + (f"+remote({extra})" if extra else "")


def classify_note(text: str | None) -> str:
    """What kind of authorship a note carries: missing/empty/mock_ai/human/ai/mixed."""
    if text is None:
        return "missing"
    if re.search(r'"tool"\s*:\s*"mock_ai"', text):
        return "mock_ai"
    tools: list[str] = []
    for session in note_sessions(text):
        agent = session.get("agent_id")
        if isinstance(agent, dict) and agent.get("tool"):
            tools.append(str(agent["tool"]))
    real_ai = [t for t in tools if t and t != "mock_ai"]
    parts = split_note(text)
    has_human = bool(parts and '"humans"' in parts[1])
    if real_ai and has_human:
        return "mixed"
    if real_ai:
        return "ai"
    if has_human:
        return "human"
    return "empty"


NOTE_KIND_LABEL = {
    "missing": "没有 note",
    "empty": "空 note",
    "mock_ai": "mock_ai（待归一化）",
    "human": "human（被当成人工提交）",
    "ai": "AI 会话",
    "mixed": "AI + human 混合",
}


def doctor_repo(repo: Path, config: dict, *, sample: int = 10) -> dict:
    """Diagnose why commits in one repository do or do not get AI attribution."""
    checks: list[dict] = []

    def add(level: str, title: str, detail: str = "") -> None:
        checks.append({"level": level, "title": title, "detail": detail})

    # 1. git-ai 二进制 / 版本 / 是否退化成 git 透传
    executable, origin = find_git_ai_with_origin()
    if not executable:
        tried = list(dict.fromkeys(why for _path, why in git_ai_search_paths()))
        add("fail", "找不到 git-ai",
            "已找过：" + "、".join(tried[:8]) + "；也可以设置 GIT_AI_EXE，或把 git-ai[.exe] 放到工具目录的 bin/ 下")
    else:
        version = git_ai_version()
        if version == DEFAULT_WHEN_UNKNOWN:
            add("fail", "git-ai 不是以 git-ai 的方式在运行",
                f"{executable} 返回的是 git 的信息（非规范大小写启动会退化成纯 git 透传）")
        else:
            add("ok", f"git-ai 可用（{version}）", f"{executable}   来源：{origin}")

    # 2. daemon：没有它 checkpoint 不会被处理
    if executable:
        status = run([executable, "bg", "status"], timeout=60)
        running = None
        try:
            payload = json.loads((status.stdout or "").strip() or "{}")
            running = bool(payload.get("daemon_running"))
        except json.JSONDecodeError:
            running = None
        if running:
            add("ok", "git-ai daemon 正在运行")
        elif running is False:
            add("fail", "git-ai daemon 没有运行", "运行 `git-ai bg start`；否则 checkpoint 不会被写入 note")
        else:
            add("warn", "无法确认 daemon 状态", (status.stderr or "").strip()[:120])

    # 3. 会被 git-ai 跳过的环境变量
    blockers = [
        key for key in (
            ENV_AUTO, "GIT_AI_SKIP_ALL_HOOKS", "GIT_AI_SKIP_MANAGED_HOOKS", "CI",
            "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE", "CIRCLECI",
            "CODEBUILD_BUILD_ID", "AGENT_OS", "KUBERNETES_SERVICE_HOST",
        ) if os.environ.get(key) and (key != ENV_AUTO or os.environ.get(key) == "0")
    ]
    if blockers:
        add("warn", "环境变量会跳过 AI 归属", ", ".join(f"{k}={os.environ.get(k)}" for k in blockers))
    else:
        add("ok", "没有会跳过 AI 归属的环境变量")

    # 4. core.hooksPath 会整体接管钩子目录
    hooks_path = git_line(["config", "core.hooksPath"], repo)
    if hooks_path:
        origin = git_line(["config", "--show-origin", "core.hooksPath"], repo, default="")
        add("fail", f"core.hooksPath = {hooks_path}",
            f"git 只会在该目录找钩子，.git/hooks 被忽略（{origin}）")
    else:
        add("ok", "core.hooksPath 未设置（使用 .git/hooks）")

    # 5. 每个钩子是否存在、是否本工具管理
    installed_any = False
    for kind in HOOK_KINDS:
        path = Path(str(repo)) / ".git" / "hooks" / kind
        if not path.is_file():
            add("fail", f"{kind} 钩子不存在", f"运行 git-ai-manager install --repo {repo}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            add("warn", f"{kind} 无法读取", str(exc))
            continue
        if BLOCK_BEGIN in text or any(m in text for m in LEGACY_MARKERS):
            installed_any = True
            if os.name != "nt" and not os.access(path, os.X_OK):
                add("fail", f"{kind} 没有执行权限", f"chmod +x {path}")
            else:
                add("ok", f"{kind} 钩子已安装")
        else:
            add("warn", f"{kind} 被其它钩子占用", "本工具安装时会保留第三方内容并追加自己的块")

    # 6. 最近若干提交的归属分布
    kinds = [classify_note(read_note(repo, sha)) for sha in
             (git_text(["log", f"-n{sample}", "--format=%H"], repo) or "").split()]
    distribution = {kind: kinds.count(kind) for kind in sorted(set(kinds))} if kinds else {}
    head = git_line(["rev-parse", "HEAD"], repo)
    head_kind = classify_note(read_note(repo, head)) if head else "missing"

    # 7. 身份信息（仅供参考，不影响归属）
    email = git_line(["config", "user.email"], repo) or "(未设置)"
    add("info", f"本机 git 身份 user.email = {email}",
        "只影响「把本地 human note 转成 AI session」这一步，不会让 AI 提交变成 human")

    # 结论
    if not installed_any:
        verdict = "没有安装提交钩子 → 提交不会被标记为 AI，是最常见的原因"
    elif head_kind == "human":
        verdict = "钩子已装但这条提交仍是 human：说明提交时 checkpoint 没被记录/认领（daemon、--no-verify 或 CI 变量）"
    elif head_kind == "missing":
        verdict = "HEAD 没有 note：提交时钩子可能没执行到，或 daemon 还没处理"
    elif head_kind == "mock_ai":
        verdict = "HEAD 是 mock_ai：钩子正常，只差归一化（normalize / sync-notes）"
    elif head_kind in ("ai", "mixed"):
        verdict = "HEAD 已经是 AI 归属，链路正常"
    else:
        verdict = "HEAD 的 note 为空，建议用 repair-empty 重建行归属"

    failures = [c for c in checks if c["level"] == "fail"]
    if failures and installed_any:
        verdict = f"{failures[0]['title']} → {verdict}"

    return {
        "repo": str(repo),
        "verdict": verdict,
        "head": head or "",
        "head_kind": head_kind,
        "distribution": distribution,
        "sample": len(kinds),
        "checks": checks,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    sample = int(getattr(args, "sample", 10) or 10)
    reports = [doctor_repo(repo, config, sample=sample) for repo in repos]
    if getattr(args, "json", False):
        info(json.dumps(reports, indent=2, ensure_ascii=False))
        return 0
    icons = {"ok": "[ok]  ", "warn": "[警告]", "fail": "[问题]", "info": "[信息]"}
    problems = 0
    for report in reports:
        info("=" * 72)
        info(f"仓库 : {report['repo']}")
        info(f"结论 : {report['verdict']}")
        info("-" * 72)
        for check in report["checks"]:
            info(f"{icons.get(check['level'], '[?]')} {check['title']}")
            if check["detail"]:
                info(f"        {check['detail']}")
            if check["level"] == "fail":
                problems += 1
        info("-" * 72)
        info(f"最近 {report['sample']} 条提交：HEAD = {NOTE_KIND_LABEL.get(report['head_kind'], report['head_kind'])}")
        for kind, count in sorted(report["distribution"].items()):
            info(f"  {NOTE_KIND_LABEL.get(kind, kind):24} {count}")
        info("")
    info("提示：human_additions 占满 = 提交时没有 AI checkpoint；")
    info("      历史 human note 可用 convert-human --apply 批量转成 AI 会话。")
    return 1 if problems else 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    rows: list[list[object]] = []
    payload: list[dict] = []
    for repo in repos:
        index = notes_index(repo, NOTES_REFNAME)
        snapshot = notes_index(repo, REMOTE_SNAPSHOT_REF)
        mock = len(grep_notes(repo, '"tool": "mock_ai"', NOTES_REFNAME)) + len(
            grep_notes(repo, '"tool":"mock_ai"', NOTES_REFNAME)
        )
        humans = len(grep_notes(repo, '"humans"', NOTES_REFNAME))
        branch = git_line(["rev-parse", "--abbrev-ref", "HEAD"], repo, default="-")
        head = git_line(["rev-parse", "--short", "HEAD"], repo, default="-")
        dirty = bool((git_text(["status", "--porcelain"], repo) or "").strip())
        tool, model = resolve_tool_model(config, repo)
        state = remote_state(repo, index, snapshot)
        rows.append(
            [
                repo.name,
                branch,
                head + ("*" if dirty else ""),
                len(index),
                mock,
                humans,
                state,
                f"{tool}::{model}",
                hook_state(repo),
            ]
        )
        payload.append(
            {
                "repo": str(repo),
                "branch": branch,
                "head": head,
                "dirty": dirty,
                "notes": len(index),
                "mock_ai": mock,
                "human": humans,
                "remote": state,
                "tool": tool,
                "model": model,
                "hooks": hook_state(repo),
            }
        )
    if getattr(args, "json", False):
        info(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    info(f"=== git-ai status ({len(repos)} repositories) ===")
    info(
        render_table(
            ["REPOSITORY", "BRANCH", "HEAD", "NOTES", "MOCK_AI", "HUMAN", "REMOTE", "TOOL::MODEL", "HOOKS"],
            rows,
        )
    )
    info("hooks legend: mgr = managed by this tool, ps1 = legacy PowerShell, other = foreign hook")
    return 0


def cmd_records(args: argparse.Namespace) -> int:
    config = load_config()
    repos = collect_repos(args, config)
    limit = int(getattr(args, "limit", 20) or 20)
    rows: list[list[object]] = []
    payload: list[dict] = []
    for repo in repos:
        index = notes_index(repo, NOTES_REFNAME)
        snapshot = notes_index(repo, REMOTE_SNAPSHOT_REF)
        result = git(
            ["log", f"-n{limit}", "--no-color", "--format=%H%x1f%h%x1f%s%x1f%an"],
            repo,
        )
        if result.returncode != 0:
            continue
        for line in (result.stdout or "").splitlines():
            fields = line.split("\x1f")
            if len(fields) < 4:
                continue
            full, short, subject, author = fields[0], fields[1], fields[2], fields[3]
            blob = index.get(full)
            tools = ""
            ai_lines = 0
            if blob:
                note = read_note(repo, full)
                if note:
                    parts = split_note(note)
                    if parts:
                        ai_lines = count_attributed_lines(parts[0])
                    tools = ", ".join(note_tools(note))
            if not blob:
                state = "remote-only" if full in snapshot else "-"
            elif not snapshot:
                state = "unknown"
            elif snapshot.get(full) == blob:
                state = "synced"
            else:
                state = "pending"
            rows.append(
                [
                    repo.name,
                    short,
                    trunc(subject, 44),
                    ai_lines,
                    tools or "-",
                    state,
                    trunc(author, 16),
                ]
            )
            payload.append(
                {
                    "repo": str(repo),
                    "commit": full,
                    "short": short,
                    "subject": subject,
                    "author": author,
                    "ai_lines": ai_lines,
                    "tools": tools,
                    "remote": state,
                }
            )
    if getattr(args, "json", False):
        info(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    info(f"=== git-ai records (last {limit} commits per repository) ===")
    info(
        render_table(
            ["REPOSITORY", "COMMIT", "SUBJECT", "AI-LINES", "TOOL::MODEL", "REMOTE", "AUTHOR"],
            rows,
        )
    )
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    tail = int(getattr(args, "tail", 50) or 50)
    records = read_logs(tail, include_all=bool(getattr(args, "all", False)))
    if getattr(args, "json", False):
        info(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    if not records:
        info(f"no log records in {log_dir()}")
        return 0
    rows = []
    for record in records:
        rows.append(
            [
                str(record.get("ts", ""))[11:19],
                str(record.get("repo", "")),
                str(record.get("commit", ""))[:12],
                str(record.get("action", "")),
                str(record.get("status", "")),
                str(record.get("detail", "")),
                str(record.get("tool", "")),
            ]
        )
    info(f"=== git-ai manager log (last {len(records)}) ===")
    info(render_table(["TIME", "REPOSITORY", "COMMIT", "ACTION", "STATUS", "DETAIL", "TOOL"], rows))
    info(f"log directory: {log_dir()}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    repo_map_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    target = config_file()
    created_config = False
    if not target.exists():
        save_config(default_config(), target)
        created_config = True
    example_source = script_dir() / EXAMPLE_NAME
    example_target = home / EXAMPLE_NAME
    if example_source.is_file() and not example_target.exists():
        try:
            shutil.copyfile(example_source, example_target)
        except OSError:
            pass
    info("=== git-ai manager init ===")
    info(f"git-ai home     : {home}")
    info(f"config file     : {target}{' (created)' if created_config else ' (kept)'}")
    info(f"repo map        : {repo_map_dir()}")
    info(f"logs            : {log_dir()}")
    info(f"python (hooks)  : {find_python(getattr(args, 'python', None))}")
    info(f"git-ai binary   : {find_git_ai() or '(not found - set GIT_AI_EXE)'}")
    roots = explicit_roots(args, load_config())
    info(f"projects roots  : {', '.join(str(r) for r in roots) if roots else '(none configured)'}")
    info("")
    info("Next steps:")
    info(f"  {APP} config add-root <your projects dir>")
    info(f"  {APP} install")
    info(f"  {APP} sync-notes")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config()
    target = config_file()
    action = args.action
    if action == "path":
        info(str(target))
        return 0
    if action == "show":
        info(json.dumps(config, indent=2, ensure_ascii=False))
        return 0
    if action == "get":
        value = deep_get(config, args.key)
        if value is None:
            warn(f"{args.key} is not set")
            return 1
        info(json.dumps(value, indent=2, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
        return 0
    if action == "set":
        deep_set(config, args.key, coerce_value(args.value))
        save_config(config, target)
        info(f"{args.key} = {args.value}")
        return 0
    if action == "unset":
        removed = deep_unset(config, args.key)
        save_config(config, target)
        info(f"unset {args.key}" if removed else f"{args.key} was not set")
        return 0
    if action in ("add-root", "remove-root"):
        roots = [str(v) for v in (config.get("projectsRoots") or [])]
        if action == "add-root":
            for value in args.value:
                path = str(expand(value).absolute())
                if not Path(path).is_dir():
                    warn(f"not a directory, skipped: {path}")
                    continue
                if norm_key(path) in {norm_key(r) for r in roots}:
                    continue
                roots.append(path)
                info(f"root added: {path}")
        else:
            for value in args.value:
                before = len(roots)
                roots = [r for r in roots if not same_path(r, value)]
                info(f"root removed: {value}" if len(roots) < before else f"root not present: {value}")
        config["projectsRoots"] = roots
        save_config(config, target)
        info(f"projectsRoots = {roots}")
        return 0
    if action == "set-repo":
        key = str(expand(args.repo_path).absolute())
        entry = config.setdefault("repos", {})
        if not isinstance(entry, dict):
            entry = {}
            config["repos"] = entry
        existing = next((k for k in entry if same_path(k, key)), None)
        if existing is None:
            existing = key
        current = entry.get(existing) if isinstance(entry.get(existing), dict) else {}
        tool = args.tool if args.tool is not None else current.get("tool")
        model = args.model if args.model is not None else current.get("model")
        payload: dict = {}
        if tool:
            payload["tool"] = tool
        if model:
            payload["model"] = model
        if not payload:
            payload = {"tool": "codex", "model": DEFAULT_WHEN_UNKNOWN}
        entry[existing] = payload
        save_config(config, target)
        info(f"repos[{existing}] = {json.dumps(payload, ensure_ascii=False)}")
        return 0
    if action == "remove-repo":
        entry = config.get("repos")
        if isinstance(entry, dict):
            for key in list(entry):
                if same_path(key, args.repo_path):
                    del entry[key]
                    info(f"removed repos entry: {key}")
        save_config(config, target)
        return 0
    raise SystemExit(f"{APP}: unknown config action {action}")


def coerce_value(value: str):
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return value


# ---------------------------------------------------------------- cli
def add_scope_arguments(parser: argparse.ArgumentParser, *, repo: bool = True) -> None:
    parser.add_argument("--root", action="append", metavar="DIR",
                        help="projects root to scan (repeatable). Defaults to $GIT_AI_PROJECT_ROOTS "
                             "then config projectsRoots")
    if repo:
        parser.add_argument("--repo", metavar="DIR", help="operate on a single repository")
    parser.add_argument("--depth", type=int, metavar="N",
                        help=f"how many directory levels below a root to search for repositories "
                             f"(default: config defaults.repoDepth, else {DEFAULT_REPO_DEPTH})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP,
        description="git-ai authorship manager: hooks, notes synchronization and repair.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            f"""\
            environment
              {ENV_HOME}       override the git-ai home directory
              {ENV_GIT_AI_EXE}   path to the git-ai executable
              {ENV_PYTHON}      python used by the installed hooks
              {ENV_CONFIG}      override the manager.json location
              {ENV_ROOTS}       projects roots ({os.pathsep} separated)
              {ENV_AUTO}       set to 0 to disable the pre-commit adapter
            """
        ),
    )
    parser.add_argument("--home", metavar="DIR", help="git-ai home (default: $GIT_AI_HOME or this script's directory)")
    parser.add_argument("--config", metavar="FILE", help="manager.json location")
    parser.add_argument("--version", action="version", version=f"{APP} (git-ai authorship helpers)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the git-ai home layout and a starter manager.json")

    cfg = sub.add_parser("config", help="read or change manager.json")
    cfg_sub = cfg.add_subparsers(dest="action", required=True)
    cfg_sub.add_parser("path", help="print the manager.json path")
    cfg_sub.add_parser("show", help="print the effective configuration")
    get_p = cfg_sub.add_parser("get", help="print one value (dot notation)")
    get_p.add_argument("key")
    set_p = cfg_sub.add_parser("set", help="set one value (dot notation)")
    set_p.add_argument("key")
    set_p.add_argument("value")
    unset_p = cfg_sub.add_parser("unset", help="remove one value")
    unset_p.add_argument("key")
    add_root = cfg_sub.add_parser("add-root", help="add projects roots")
    add_root.add_argument("value", nargs="+")
    del_root = cfg_sub.add_parser("remove-root", help="remove projects roots")
    del_root.add_argument("value", nargs="+")
    set_repo = cfg_sub.add_parser("set-repo", help="set the tool/model for one repository")
    set_repo.add_argument("repo_path")
    set_repo.add_argument("--tool")
    set_repo.add_argument("--model")
    del_repo = cfg_sub.add_parser("remove-repo", help="remove a repository override")
    del_repo.add_argument("repo_path")

    inst = sub.add_parser("install", help="install the pre/post-commit hooks and the git-ai hook")
    add_scope_arguments(inst)
    inst.add_argument("--python", help="interpreter the hooks should call")
    inst.add_argument("--dry-run", action="store_true")
    inst.add_argument("--no-git-ai-config", action="store_true", help="do not touch git_ai_hooks.post_notes_updated")
    inst.add_argument("--no-daemon-restart", action="store_true")
    inst.add_argument("--legacy", action="store_true", help="write the PowerShell based hooks instead")
    inst.add_argument("--json", action="store_true", help="machine readable summary")

    unin = sub.add_parser("uninstall", help="remove the managed hook blocks")
    add_scope_arguments(unin)
    unin.add_argument("--python", help="interpreter used when restoring legacy hooks")
    unin.add_argument("--dry-run", action="store_true")
    unin.add_argument("--keep-git-ai-config", action="store_true")
    unin.add_argument("--json", action="store_true", help="machine readable summary")

    hook = sub.add_parser("hook", help="hook entry points (called by git)")
    hook.add_argument("kind", choices=["pre-commit", "post-commit", "normalize-later"])
    hook.add_argument("--repo", help="repository path (normalize-later)")
    hook.add_argument("--commit", help="commit sha (normalize-later)")
    hook.add_argument("--wait", type=float, help="seconds to wait for the note (normalize-later)")

    sub.add_parser("notes-updated", help="git-ai post_notes_updated hook (reads JSON on stdin)")

    sync = sub.add_parser("sync-notes", help="normalize, fetch, merge and push refs/notes/ai")
    add_scope_arguments(sync)
    sync.add_argument("--remote", help="remote name (default from config, else origin)")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--no-push", action="store_true", help="merge but do not push")
    sync.add_argument("--normalize", dest="normalize", action="store_true", default=True,
                      help="normalize local notes before pushing (default)")
    sync.add_argument("--no-normalize", dest="normalize", action="store_false",
                      help="push whatever is in refs/notes/ai untouched")
    sync.add_argument("--normalize-limit", type=int, metavar="N",
                      help="cap the normalization pass at N notes per repository")
    sync.add_argument("--json", action="store_true", help="machine readable result")

    refresh = sub.add_parser("refresh-notes", help="re-apply normalization to every note of a repository")
    add_scope_arguments(refresh)
    refresh.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    refresh.add_argument("--include-human", dest="include_human", action="store_true", default=None)
    refresh.add_argument("--no-include-human", dest="include_human", action="store_false")
    refresh.add_argument("--email", action="append", metavar="ADDR", help="author filter (repeatable)")
    refresh.add_argument("--limit", type=int, help="stop after N notes per repository")

    norm = sub.add_parser("normalize", help="rewrite historical mock_ai notes to the configured tool/model")
    add_scope_arguments(norm)
    norm.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    norm.add_argument("--include-human", action="store_true", help="also convert local human notes")
    norm.add_argument("--email", action="append", metavar="ADDR", help="author filter (repeatable)")
    norm.add_argument("--subject-pattern", metavar="REGEX", help="only convert commits whose subject matches")
    norm.add_argument("--limit", type=int)
    norm.add_argument("--json", action="store_true", help="machine readable summary")

    conv = sub.add_parser("convert-human", help="rewrite historical human notes into AI sessions")
    add_scope_arguments(conv)
    conv.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    conv.add_argument("--email", action="append", metavar="ADDR", help="author filter (repeatable)")
    conv.add_argument("--subject-pattern", metavar="REGEX", help="only convert commits whose subject matches")
    conv.add_argument("--limit", type=int)

    repair = sub.add_parser("repair-empty", help="rebuild line attribution for commits that have none")
    repair.add_argument("--repo", metavar="DIR", help="repository (default: current directory)")
    repair.add_argument("--commits", action="append", metavar="SHA", help="commit(s), comma separated, repeatable")
    repair.add_argument("--scan", action="store_true", help="find notes without line attribution automatically")
    repair.add_argument("--scan-missing", dest="scan_missing", action="store_true",
                        help="find commits that have no note at all (history from before git-ai)")
    repair.add_argument("--branch", metavar="REV",
                        help="history to scan with --scan-missing (branch, tag or A..B; default HEAD)")
    repair.add_argument("--since", metavar="DATE",
                        help="only consider commits newer than DATE, e.g. 2025-01-01 or '6 months ago'")
    repair.add_argument("--until", metavar="DATE",
                        help="only consider commits older than DATE, e.g. 2026-04-01")
    repair.add_argument("--limit", type=int, metavar="N",
                        help=f"process at most N commits (default {DEFAULT_BACKFILL_LIMIT}; 0 = no limit)")
    repair.add_argument("--count-only", dest="count_only", action="store_true",
                        help="with --scan-missing: only report how many commits lack a note, then stop")
    repair.add_argument("--apply", action="store_true", help="write notes (default: dry-run)")
    repair.add_argument("--force", action="store_true", help="overwrite notes that already have attribution")
    repair.add_argument("--show", action="store_true", help="print the generated note in dry-run mode")
    repair.add_argument("--json", action="store_true", help="machine readable summary")

    status = sub.add_parser("status", help="per repository authorship summary")
    add_scope_arguments(status)
    status.add_argument("--json", action="store_true")

    doc = sub.add_parser("doctor", help="诊断为什么提交没有被记为 AI")
    add_scope_arguments(doc)
    doc.add_argument("--sample", type=int, default=10, metavar="N",
                     help="检查最近 N 条提交的归属分布（默认 10）")
    doc.add_argument("--json", action="store_true")

    records = sub.add_parser("records", help="recent commits with AI line counts and sync state")
    add_scope_arguments(records)
    records.add_argument("--limit", type=int, default=20, help="commits per repository (default 20)")
    records.add_argument("--json", action="store_true")

    logs = sub.add_parser("logs", help="tail the manager JSONL log")
    logs.add_argument("--tail", type=int, default=50)
    logs.add_argument("--all", action="store_true", help="read every log file, not just today's")
    logs.add_argument("--json", action="store_true")

    return parser


def apply_global_overrides(args: argparse.Namespace) -> None:
    if getattr(args, "home", None):
        os.environ[ENV_HOME] = str(expand(args.home))
    if getattr(args, "config", None):
        os.environ[ENV_CONFIG] = str(expand(args.config))


def main(argv: list[str] | None = None) -> int:
    ensure_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    apply_global_overrides(args)
    command = args.command
    try:
        if command == "init":
            return cmd_init(args)
        if command == "config":
            return cmd_config(args)
        if command == "install":
            return cmd_install(args)
        if command == "uninstall":
            return cmd_uninstall(args)
        if command == "hook":
            if args.kind == "pre-commit":
                return cmd_hook_pre_commit(args)
            if args.kind == "post-commit":
                return cmd_hook_post_commit(args)
            return cmd_hook_normalize_later(args)
        if command == "notes-updated":
            return cmd_notes_updated(args)
        if command == "sync-notes":
            return cmd_sync_notes(args)
        if command == "refresh-notes":
            return cmd_refresh_notes(args)
        if command == "normalize":
            return cmd_normalize(args)
        if command == "convert-human":
            return cmd_convert_human(args)
        if command == "repair-empty":
            return cmd_repair_empty(args)
        if command == "status":
            return cmd_status(args)
        if command == "doctor":
            return cmd_doctor(args)
        if command == "records":
            return cmd_records(args)
        if command == "logs":
            return cmd_logs(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        warn("interrupted")
        return 130
    except Exception as exc:  # pragma: no cover - defensive
        if command in ("hook", "notes-updated"):
            warn(f"{command}: {exc!r}")
            return 0
        raise
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
