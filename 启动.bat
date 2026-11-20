@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem R08：端口 / 主机 / 数据目录由 .env 与环境变量统一决定，自检和服务用同一份
for /f "delims=" %%A in ('python -c "from backend.app.runtime import cli_args; print(cli_args())"') do set "UV_ARGS=%%A"
for /f "delims=" %%B in ('python -c "from backend.app.runtime import url; print(url())"') do set "APP_URL=%%B"
if "%UV_ARGS%"=="" (
  echo 读不到启动配置：请确认用的是本项目的 Python，并先装好依赖。
  echo 可用解释器：python -c "import sys; print(sys.executable)"
  echo 安装依赖：python -m pip install -r backend\requirements.txt
  pause
  exit /b 1
)

echo ============================================
echo   TenderForge · 标书锻造
echo   启动后浏览器打开 %APP_URL%
echo ============================================
python -m backend.app.preflight
if errorlevel 1 (
  echo.
  echo 自检没通过，先按上面的「怎么办」处理，再双击本文件启动。
  pause
  exit /b 1
)
echo.
python -m uvicorn backend.app.main:app %UV_ARGS%
pause
