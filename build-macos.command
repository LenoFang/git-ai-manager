#!/bin/bash
# ---------------------------------------------------------------
#  在 macOS 上打包成 .app（目标机器不需要装 Python）
#  需要：python3 + tkinter（python.org 版自带；Homebrew 用 brew install python-tk）
#  用法：双击本文件，或终端里 ./build-macos.command
#  产物：dist/git-ai-manager.app     分发时整个 .app 发过去
# ---------------------------------------------------------------
set -e
chmod +x "$0" 2>/dev/null || true   # 从 Windows 提交过来时可能丢了可执行位
cd "$(dirname "$0")"

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.10+（python.org 版自带 tkinter）"
  exit 1
fi
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "当前 python3 没有 tkinter：brew install python-tk"
  exit 1
fi

echo "[1/3] 准备 pyinstaller ..."
"$PY" -c "import PyInstaller" 2>/dev/null || "$PY" -m pip install --upgrade --user pyinstaller

echo "[2/3] 开始打包 ..."
# entry.py 是入口：带参数只加载命令行核心，不带参数才加载 tkinter，
# 这样提交钩子不必为图形界面付出启动开销。
# 两个 .py 以数据文件方式打包，所以 tkinter 要用 --hidden-import 显式引入。
"$PY" -m PyInstaller --noconfirm --clean --windowed --name "git-ai-manager" \
  --add-data "git-ai-manager.py:." \
  --add-data "git-ai-manager-gui.py:." \
  --hidden-import tkinter --hidden-import tkinter.ttk \
  --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox \
  --hidden-import tkinter.font \
  entry.py

echo "[3/3] 复制说明文档 ..."
mkdir -p dist
cp -f *.md manager.example.json dist/

APPBIN="$(pwd)/dist/git-ai-manager.app/Contents/MacOS/git-ai-manager"

echo
echo "打包完成： $(pwd)/dist/git-ai-manager.app"
echo
echo "macOS 上没有 Windows 那种「控制台/窗口」子系统之分，"
echo "所以同一个 .app 既能开界面，也能当命令行用："
echo
echo "  开界面：   open dist/git-ai-manager.app        （或双击）"
echo "  命令行：   \"$APPBIN\" status --json"
echo "             \"$APPBIN\" repair-empty --repo <路径> --scan-missing --count-only --json"
echo "  自检：     \"$APPBIN\" --selftest"
echo
echo "分发方式：把 dist/git-ai-manager.app 和 dist/*.md 一起发出去。"
echo "注意：首次打开会被 Gatekeeper 拦（未签名）——右键 → 打开 即可（命令行方式不受影响）。"
echo "配置和日志写在 .app 所在目录，不会写进 .app 包内部。"