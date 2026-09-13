@echo off
rem 双击启动图形界面（源码方式；打包的 exe 可能被杀软拦）
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "git-ai-manager-gui.py"
) else if exist ".venv\Scripts\python.exe" (
  start "" ".venv\Scripts\python.exe" "git-ai-manager-gui.py"
) else (
  start "" pythonw "git-ai-manager-gui.py"
)