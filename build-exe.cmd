@echo off
rem ---------------------------------------------------------------
rem  Build a standalone git-ai-manager.exe (no Python needed on the
rem  target machine).  Requires Python 3.10+; pyinstaller auto-installs.
rem
rem  Output goes to the first usable folder among: dist, dist-new, dist-out
rem  (a folder locked by antivirus or a running instance is skipped).
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "DISTPATH="
for %%D in (dist dist-new dist-out) do (
  if not defined DISTPATH (
    if exist "%%D\git-ai-manager" rmdir /s /q "%%D\git-ai-manager" 2>nul
    if not exist "%%D\git-ai-manager" set "DISTPATH=%%D"
  )
)
if not defined DISTPATH set "DISTPATH=dist-out"
echo [build] output folder: %DISTPATH%

echo [1/3] checking pyinstaller ...
python -c "import PyInstaller" 2>nul
if errorlevel 1 python -m pip install --upgrade pyinstaller || goto :fail

echo [2/3] building ...
python -m PyInstaller --noconfirm --clean --windowed --name git-ai-manager --distpath "%DISTPATH%" --add-data "git-ai-manager.py;." --add-data "git-ai-manager-gui.py;." --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox --hidden-import tkinter.font entry.py
if errorlevel 1 goto :fail

echo [3/3] copying docs ...
copy /y *.md "%DISTPATH%\git-ai-manager\" >nul 2>nul
copy /y manager.example.json "%DISTPATH%\git-ai-manager\" >nul 2>nul

echo.
echo Done: %cd%\%DISTPATH%\git-ai-manager\git-ai-manager.exe
echo Self test: %DISTPATH%\git-ai-manager\git-ai-manager.exe --selftest
echo.
echo Ship the whole %DISTPATH%\git-ai-manager folder, not just the exe.
goto :eof

:fail
echo.
echo Build failed.  "access denied" / WinError 5 usually means antivirus is
echo holding files - reboot and run this script again.
exit /b 1
