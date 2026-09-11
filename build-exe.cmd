@echo off
rem ---------------------------------------------------------------
rem  Build a standalone git-ai-manager.exe (no Python needed on the
rem  target machine).  Requires Python 3.10+; pyinstaller is installed
rem  automatically.  Output: dist\git-ai-manager\  (ship that folder)
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo [1/3] installing pyinstaller ...
  python -m pip install --upgrade pyinstaller || goto :fail
) else (
  echo [1/3] pyinstaller present
)

echo [2/3] building ...
rem entry.py dispatches: with arguments it loads only the CLI core,
rem without arguments it loads tkinter (so commit hooks stay fast).
rem Both .py files are bundled as data, so tkinter must be pulled in
rem explicitly with --hidden-import.
python -m PyInstaller --noconfirm --clean --windowed --name git-ai-manager ^
  --add-data "git-ai-manager.py;." ^
  --add-data "git-ai-manager-gui.py;." ^
  --hidden-import tkinter --hidden-import tkinter.ttk ^
  --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox ^
  --hidden-import tkinter.font ^
  entry.py || goto :fail

echo [3/3] copying docs ...
rem *.md avoids non-ASCII literals in this .cmd (cmd.exe reads it in the OEM codepage)
copy /y *.md dist\git-ai-manager\ >nul 2>nul
copy /y manager.example.json dist\git-ai-manager\ >nul 2>nul

echo.
echo Done: %cd%\dist\git-ai-manager\git-ai-manager.exe
echo Self test: dist\git-ai-manager\git-ai-manager.exe --selftest
echo.
echo Ship the whole dist\git-ai-manager folder, not just the exe.
goto :eof

:fail
echo.
echo Build failed, see the output above.
exit /b 1