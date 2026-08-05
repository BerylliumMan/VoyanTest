@echo off
chcp 65001 >nul
echo ========================================
echo  VoyanTest Agent - Offline Build [GUI]
echo ========================================
echo.

echo [1/4] Installing browser-use extensions cache offline...
set "EXT_SRC=%~dp0browseruse_extensions"
set "EXT_DST=%USERPROFILE%\.config\browseruse\extensions"
if not exist "%EXT_SRC%\ddkjiahejlhfcafbddmgiahcphecmpfh\manifest.json" (
    echo [WARN] browseruse_extensions missing - first run may need online download.
) else (
    if not exist "%EXT_DST%" mkdir "%EXT_DST%"
    xcopy /E /I /Y "%EXT_SRC%\*" "%EXT_DST%\" >nul
    echo Extensions installed to %EXT_DST%
)
echo.

echo [2/4] Installing dependencies from local wheels...
REM Top-level + Windows PyInstaller helpers (pefile / pywin32-ctypes) + jaraco for pkg_resources.
python -m pip install --no-index --find-links=wheels --no-warn-script-location ^
  httpx websockets openpyxl pyinstaller pydantic rich customtkinter pystray pillow ^
  browser-use playwright openai setuptools ^
  jaraco.text jaraco.functools jaraco.context jaraco.collections more-itertools ^
  pefile pywin32-ctypes colorama darkdetect packaging altgraph pyinstaller-hooks-contrib ^
  backports.tarfile typer-slim autocommand
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies from wheels\
    echo Make sure wheels\ is complete. See requirements_agent.txt
    pause
    exit /b 1
)
echo Done
echo.

echo [2b/4] Verifying browser-use system_prompts...
python -c "import browser_use.agent.system_prompts; print('system_prompts OK')"
if %errorlevel% neq 0 (
    echo [ERROR] browser-use installed but system_prompts missing. Re-copy wheels and reinstall.
    pause
    exit /b 1
)
echo.

echo [3/4] Building Agent GUI...
if exist VoyanTest-Agent.spec del VoyanTest-Agent.spec
REM CRITICAL: never pack agent\dist (old exe) into the new build — size balloons to 600MB+.
if exist "%~dp0agent\dist" (
    echo Removing agent\dist so old exe is not nested into the bundle...
    rmdir /s /q "%~dp0agent\dist"
)
if exist "%~dp0dist\VoyanTest-Agent" rmdir /s /q "%~dp0dist\VoyanTest-Agent"
if exist "%~dp0dist\VoyanTest-Agent.exe" del /f /q "%~dp0dist\VoyanTest-Agent.exe"

REM onedir: fast startup (onefile extracts 100MB+ to %%TEMP%% every launch).
REM chromium / node.exe stay beside the folder — do not bundle them.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name VoyanTest-Agent ^
  --exclude-module pymupdf ^
  --exclude-module fitz ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module MySQLdb ^
  --exclude-module mysql ^
  --exclude-module tkinter.test ^
  --hidden-import agent.models ^
  --hidden-import agent.client_core ^
  --hidden-import agent.cli_entry ^
  --hidden-import agent.gui.app ^
  --hidden-import agent.gui.config_dialog ^
  --hidden-import agent.gui.config_store ^
  --hidden-import core.browser_use_exec ^
  --hidden-import core.browser_use_prompts ^
  --hidden-import core.mcp_tabs ^
  --hidden-import core.locator_memory ^
  --hidden-import core.step_intent ^
  --hidden-import core.script_templates ^
  --hidden-import pydantic ^
  --hidden-import browser_use ^
  --hidden-import browser_use.agent.system_prompts ^
  --hidden-import customtkinter ^
  --hidden-import pystray ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import jaraco ^
  --hidden-import jaraco.text ^
  --hidden-import jaraco.functools ^
  --hidden-import jaraco.context ^
  --hidden-import jaraco.collections ^
  --hidden-import more_itertools ^
  --hidden-import pkg_resources ^
  --hidden-import pefile ^
  --hidden-import win32ctypes ^
  --hidden-import win32ctypes.pywin32 ^
  --collect-all browser_use ^
  --collect-all customtkinter ^
  --collect-all jaraco ^
  --collect-all setuptools ^
  --copy-metadata browser-use ^
  --copy-metadata jaraco.text ^
  --copy-metadata setuptools ^
  --add-data "agent\gui;agent\gui" ^
  --add-data "agent\__init__.py;agent" ^
  --add-data "agent\models.py;agent" ^
  --add-data "agent\client_core.py;agent" ^
  --add-data "agent\cli_entry.py;agent" ^
  --add-data "core\browser_use_exec.py;core" ^
  --add-data "core\mcp_tabs.py;core" ^
  --add-data "core\locator_memory.py;core" ^
  --add-data "core\step_intent.py;core" ^
  --add-data "core\script_templates.py;core" ^
  --add-data "core\browser_use_prompts;core\browser_use_prompts" ^
  --add-data "core\__init__.py;core" ^
  agent\gui\app.py
if %errorlevel% neq 0 (
    echo [ERROR] Build failed
    pause
    exit /b 1
)
echo Done
echo.

echo [4/4] Cleaning up...
if exist VoyanTest-Agent.spec del VoyanTest-Agent.spec
if exist build rmdir /s /q build

echo.
echo ========================================
echo  Build successful!
echo  Output: dist\VoyanTest-Agent\VoyanTest-Agent.exe  [onedir, fast start]
echo  Copy chromium / node.exe / node_modules next to that folder
echo    (same layout as release\VoyanTest-Agent\).
echo  Extensions cache: %%USERPROFILE%%\.config\browseruse\extensions
echo ========================================
pause
