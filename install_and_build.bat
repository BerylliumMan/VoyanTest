@echo off
chcp 65001 >nul
echo ========================================
echo  VoyanTest Agent - Offline Build
echo ========================================
echo.

echo [1/3] Installing dependencies from local wheels...
python -m pip install --no-index --find-links=wheels --no-warn-script-location httpx websockets openpyxl pyinstaller pydantic
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)
echo Done
echo.

echo [2/3] Building Agent...
if exist VoyanTest-Agent.spec del VoyanTest-Agent.spec
python -m PyInstaller --onefile --console --name VoyanTest-Agent --hidden-import agent.models --hidden-import pydantic agent\client.py
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
echo ========================================
pause
