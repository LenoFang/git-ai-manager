@echo off
rem ---------------------------------------------------------------
rem  Builds BOTH executables (no Python needed on the target machine):
rem
rem   git-ai-manager.exe      windowed: GUI + commit hooks (no console flash)
rem   git-ai-manager-cli.exe  console : terminal / script / AI agent use
rem
rem  Requires Python 3.10+; pyinstaller installs automatically.
rem  Output folder: first usable among dist, dist-new, dist-out, dist-cli, dist-out2.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "DISTPATH="
for %%D in (dist dist-new dist-out dist-cli dist-out2) do (
  if not defined DISTPATH (
    if exist "%%D\git-ai-manager" rmdir /s /q "%%D\git-ai-manager" 2>nul
    if not exist "%%D\git-ai-manager" set "DISTPATH=%%D"
  )
)
if not defined DISTPATH set "DISTPATH=dist-fallback"
echo [build] output folder: %DISTPATH%

echo [1/4] checking pyinstaller ...
python -c "import PyInstaller" 2>nul
if errorlevel 1 python -m pip install --upgrade pyinstaller || goto :fail

echo [2/4] building windowed exe (GUI + hooks) ...
python -m PyInstaller --noconfirm --clean --windowed --name git-ai-manager --distpath "%DISTPATH%" --add-data "git-ai-manager.py;." --add-data "git-ai-manager-gui.py;." --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox --hidden-import tkinter.font entry.py
if errorlevel 1 goto :fail

echo [3/4] building console exe (terminal / agent) ...
python -m PyInstaller --noconfirm --clean --console --onefile --name git-ai-manager-cli --distpath "%DISTPATH%\cli-tmp" --add-data "git-ai-manager.py;." --add-data "git-ai-manager-gui.py;." entry.py
if errorlevel 1 goto :fail
move /y "%DISTPATH%\cli-tmp\git-ai-manager-cli.exe" "%DISTPATH%\git-ai-manager\" >nul
rmdir /s /q "%DISTPATH%\cli-tmp" 2>nul

echo [4/4] copying docs ...
copy /y *.md "%DISTPATH%\git-ai-manager\" >nul 2>nul
copy /y manager.example.json "%DISTPATH%\git-ai-manager\" >nul 2>nul

echo.
echo GUI   : %cd%\%DISTPATH%\git-ai-manager\git-ai-manager.exe
echo CLI   : %cd%\%DISTPATH%\git-ai-manager\git-ai-manager-cli.exe
echo Verify: %DISTPATH%\git-ai-manager\git-ai-manager.exe --selftest
echo         %DISTPATH%\git-ai-manager\git-ai-manager-cli.exe --version
goto :eof

:fail
echo.
echo Build failed - access denied / WinError 5 usually means antivirus is
echo holding files.  Reboot and run this script again.
exit /b 1
