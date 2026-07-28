@echo off
chcp 65001 >nul
echo ========================================
echo  VoyanTest Agent - Offline Build
echo ========================================
echo.

echo [1/3] Installing dependencies from local wheels...
python -m pip install --no-index --find-links=wheels --no-warn-script-location ^
  httpx websockets openpyxl pyinstaller pydantic rich customtkinter pystray pillow ^
  browser-use playwright openai
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies from wheels\
    echo Make sure wheels\ contains browser-use and its dependencies.
    pause
    exit /b 1
)
echo Done
echo.

echo [2/3] Building Agent...
if exist VoyanTest-Agent.spec del VoyanTest-Agent.spec
python -m PyInstaller --onefile --console --name VoyanTest-Agent ^
  --hidden-import agent.models ^
  --hidden-import agent.client_core ^
  --hidden-import core.browser_use_exec ^
  --hidden-import pydantic ^
  --hidden-import browser_use ^
  --add-data "agent;agent" ^
  --add-data "core\browser_use_exec.py;core" ^
  --add-data "core\__init__.py;core" ^
  agent\client.py
if %errorlevel% neq 0 (
    echo [ERROR] Build failed
    pause
    exit /b 1
)
echo Done
echo.

echo [3/3] Cleaning up...
if exist VoyanTest-Agent.spec del VoyanTest-Agent.spec
if exist build rmdir /s /q build

echo.
echo ========================================
echo  Build successful!
echo  Output: dist\VoyanTest-Agent.exe
echo  Also keep node.exe / node_modules / chromium next to the exe.
echo ========================================
pause
