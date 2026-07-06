@echo off
REM VoyanTest Agent Windows build script

echo ========================================
echo Installing Python dependencies...
echo ========================================

pip install ^
  --no-index --find-links=wheels ^
  pydantic pydantic-core annotated-types httpx httpx-sse websockets rich anyio ^
  httpcore sniffio h11 certifi idna typing_extensions

if %ERRORLEVEL% neq 0 (
    echo pip install failed
    pause
    exit /b 1
)

echo ========================================
echo Building executable with PyInstaller...
echo ========================================

pyinstaller --clean --onefile --name VoyanTest-Agent ^
  --hidden-import httpx ^
  --hidden-import websockets ^
  --hidden-import pydantic ^
  --hidden-import rich ^
  --hidden-import httpx_sse ^
  agent/client.py

echo ========================================
echo Build complete!
echo Output: dist\VoyanTest-Agent.exe
echo ========================================
pause

