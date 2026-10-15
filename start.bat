@echo off
rem PerfScope 一键启动（Windows）
rem   start.bat          连接真实手机
rem   start.bat --demo   没有手机时用模拟设备体验
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "ROOT=%cd%"
if "%SERVER_PORT%"=="" set SERVER_PORT=9000
if "%CLIENT_PORT%"=="" set CLIENT_PORT=8765

where python >nul 2>nul
if errorlevel 1 (
  echo 没有找到 Python。请从 https://www.python.org/downloads/ 安装 Python 3.10 或更新版本，
  echo 安装时勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo 需要 Python 3.10 或更新版本。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 首次运行：创建虚拟环境…
  python -m venv .venv
)
set "VPY=%ROOT%\.venv\Scripts\python.exe"
rem server.txt 里写了团队服务端地址时，只启动客户端，数据直接上传到那里（发给同事的试用包就是这样配置的）
set "REMOTE_SERVER="
if exist "%ROOT%\server.txt" set /p REMOTE_SERVER=<"%ROOT%\server.txt"
set "REQS=-r client\requirements.txt"
if "%REMOTE_SERVER%"=="" set "REQS=-r server\requirements.txt -r client\requirements.txt"
echo 检查依赖…
"%VPY%" -m pip install -q --disable-pip-version-check %REQS%
if errorlevel 1 (
  echo 依赖安装失败，请检查网络。
  pause
  exit /b 1
)

if /i "%1"=="--demo" (
  set "ADB_PATH=%ROOT%\tools\fake_adb.py"
  echo 演示模式：使用模拟设备
) else (
  where adb >nul 2>nul
  if errorlevel 1 if "%ADB_PATH%"=="" (
    echo 提示：没有找到 adb。连接手机测试需要安装 Android platform-tools：
    echo   https://developer.android.com/tools/releases/platform-tools
    echo   解压后把目录加入 PATH，或设置环境变量 ADB_PATH 指向 adb.exe
    echo 没有手机也可以先用演示模式：start.bat --demo
  )
)

netstat -ano | findstr /r /c:":%CLIENT_PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo 客户端端口 %CLIENT_PORT% 已被占用（可能 PerfScope 已经在运行，或被其他程序使用）。
  echo 可以关闭占用的程序，或换一个端口，例如先执行 set CLIENT_PORT=8766 再运行 start.bat
  pause
  exit /b 1
)

if not "%REMOTE_SERVER%"=="" goto remote

netstat -ano | findstr /r /c:":%SERVER_PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo 服务端端口 %SERVER_PORT% 已被占用（可能 PerfScope 已经在运行，或被其他程序使用）。
  echo 可以关闭占用的程序，或换一个端口，例如先执行 set SERVER_PORT=9001 再运行 start.bat
  pause
  exit /b 1
)
set "PERFSCOPE_SERVER_URL=http://127.0.0.1:%SERVER_PORT%"
if not exist "%ROOT%\data" mkdir "%ROOT%\data"
set "DATABASE_URL=sqlite:///%ROOT:\=/%/data/perfscope.db"
set "BLOB_DIR=%ROOT%\data\blobs"
echo 启动服务端：http://127.0.0.1:%SERVER_PORT% （在单独的窗口中运行，关闭该窗口即停止）
start "PerfScope 服务端" /min /d "%ROOT%\server" "%VPY%" -m uvicorn perfserver.main:app --host 127.0.0.1 --port %SERVER_PORT% --log-level warning
timeout /t 3 /nobreak >nul
goto client

:remote
set "PERFSCOPE_SERVER_URL=%REMOTE_SERVER%"
echo 团队服务端：%REMOTE_SERVER% （测试数据上传到这里，在浏览器打开这个地址查看）
"%VPY%" -c "import urllib.request; urllib.request.urlopen('%REMOTE_SERVER%/api/me', timeout=3)" 2>nul
if errorlevel 1 echo   暂时连不上团队服务端：请确认和服务端在同一网络（公司 Wi-Fi / VPN）。不影响本地采集，连上后再上传即可。

:client
echo 启动客户端：http://127.0.0.1:%CLIENT_PORT% （关闭本窗口即停止客户端）
cd /d "%ROOT%\client"
"%VPY%" -m perfclient serve --port %CLIENT_PORT%
endlocal
