#!/usr/bin/env bash
# PerfScope 一键启动（macOS / Linux；macOS 上也可以直接双击 PerfScope.command）
#   ./start.sh          连接真实手机
#   ./start.sh --demo   没有手机时用模拟设备体验
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
SERVER_PORT="${SERVER_PORT:-9000}"
# 给同事试用：SHARE=1 ./start.sh 让服务端监听局域网，并用本机局域网 IP 作为对外地址
# （客户端始终只在本机访问，它控制的是插在这台电脑上的手机）
if [ "$SHARE" = "1" ]; then
  SERVER_HOST="${SERVER_HOST:-0.0.0.0}"
  PUBLIC_HOST="${PUBLIC_HOST:-$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')}"
fi
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"
CLIENT_PORT_SET="$CLIENT_PORT"
CLIENT_PORT="${CLIENT_PORT:-8765}"

install_hint() {
  if command -v brew >/dev/null 2>&1; then echo "  在终端运行：brew install $1"
  else echo "  下载安装：$2"; fi
}

# macOS 自带的 /usr/bin/python3 只是个占位程序，没装开发者工具时一运行就会弹窗，所以优先找真正装好的 Python
if [ -z "$PYTHON" ]; then
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
           /opt/homebrew/bin/python3.1[0-9] /usr/local/bin/python3.1[0-9]; do   # brew python@3.x 可能只有带版本号的命令
    [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null && { PYTHON="$c"; break; }
  done
fi
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1 || \
   { [ "$(command -v "$PY")" = /usr/bin/python3 ] && [ "$(uname)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; }; then
  echo "没有找到 Python 3，请先安装 Python 3.10 或更新版本，装好后重新启动 PerfScope："
  install_hint python@3.12 https://www.python.org/downloads/
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "需要 Python 3.10 或更新版本，当前是 $("$PY" --version)"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "首次运行：创建虚拟环境…"
  "$PY" -m venv .venv
fi
VPY="$ROOT/.venv/bin/python"
echo "检查依赖…"
REQS="-r client/requirements.txt"
[ -s "$ROOT/server.txt" ] || REQS="-r server/requirements.txt $REQS"
"$VPY" -m pip install -q --disable-pip-version-check $REQS

if [ "$1" = "--demo" ]; then
  export ADB_PATH="$ROOT/tools/fake_adb.py"
  echo "演示模式：使用模拟设备"
elif ! command -v adb >/dev/null 2>&1 && [ -z "$ADB_PATH" ]; then
  echo "提示：没有找到 adb，测 Android 手机需要先安装（只测 iPhone 可以忽略）："
  install_hint android-platform-tools https://developer.android.com/tools/releases/platform-tools
  echo "没有手机也可以先用演示模式：./start.sh --demo"
fi

port_busy() { "$VPY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1', $1)) == 0 else 1)"; }

# server.txt 里写了团队服务端地址时，只启动客户端，数据直接上传到那里（发给同事的试用包就是这样配置的）
REMOTE_SERVER=""
[ -f "$ROOT/server.txt" ] && REMOTE_SERVER="$(head -1 "$ROOT/server.txt" | tr -d '[:space:]')"

# 没指定客户端端口且 8765 被占用（比如这台电脑上已经开着另一份 PerfScope）时，自动换下一个空闲端口
if [ -z "$CLIENT_PORT_SET" ]; then
  for i in $(seq 0 20); do port_busy "$CLIENT_PORT" || break; CLIENT_PORT=$((CLIENT_PORT + 1)); done
fi
checks="客户端:$CLIENT_PORT:CLIENT_PORT"
[ -z "$REMOTE_SERVER" ] && checks="服务端:$SERVER_PORT:SERVER_PORT $checks"
for pair in $checks; do
  IFS=: read -r name port var <<< "$pair"
  if port_busy "$port"; then
    echo "$name端口 $port 已被占用（可能 PerfScope 已经在运行，或被其他程序使用）。"
    echo "可以关闭占用的程序，或换一个端口，例如：$var=$((port + 1)) ./start.sh $1"
    exit 1
  fi
done

SERVER_PID=""
trap '[ -n "$SERVER_PID" ] && kill $SERVER_PID 2>/dev/null; echo; echo "已停止"' EXIT INT TERM

if [ -n "$REMOTE_SERVER" ]; then
  export PERFSCOPE_SERVER_URL="$REMOTE_SERVER"
  echo "团队服务端：$REMOTE_SERVER（测试数据上传到这里，在浏览器打开这个地址查看）"
  if ! "$VPY" -c "import urllib.request; urllib.request.urlopen('$REMOTE_SERVER/api/me', timeout=3)" 2>/dev/null; then
    echo "  暂时连不上团队服务端：请确认和服务端在同一网络（公司 Wi-Fi / VPN）。不影响本地采集，连上后再上传即可。"
  fi
else
  export PERFSCOPE_SERVER_URL="http://$PUBLIC_HOST:$SERVER_PORT"
  mkdir -p "$ROOT/data"
  echo "启动服务端：http://$PUBLIC_HOST:$SERVER_PORT"
  [ "$SERVER_HOST" != "127.0.0.1" ] && echo "  局域网内的同事可以用这个地址打开（目前没有登录认证，同一网络的人都能查看和上传）"
  (cd server && PUBLIC_URL="http://$PUBLIC_HOST:$SERVER_PORT" DATABASE_URL="sqlite:///$ROOT/data/perfscope.db" BLOB_DIR="$ROOT/data/blobs" \
    "$VPY" -m uvicorn perfserver.main:app --host "$SERVER_HOST" --port "$SERVER_PORT" \
    --log-level warning > "$ROOT/data/server.log" 2>&1) &
  SERVER_PID=$!
  for i in $(seq 1 30); do
    if "$VPY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$SERVER_PORT/api/me', timeout=1)" 2>/dev/null; then
      break
    fi
    if ! kill -0 $SERVER_PID 2>/dev/null; then
      echo "服务端启动失败，日志：$ROOT/data/server.log"; tail -20 "$ROOT/data/server.log"; exit 1
    fi
    sleep 0.5
  done
fi

echo "启动客户端：http://127.0.0.1:$CLIENT_PORT （按 Ctrl+C 停止）"
cd client
"$VPY" -m perfclient serve --port "$CLIENT_PORT" $NO_BROWSER
