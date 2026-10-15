#!/usr/bin/env bash
# 把这台 Mac 当作 PerfScope 服务器：作为后台服务常驻运行，开机自动启动，异常退出自动重启。
#   tools/service-mac.sh install     安装为开机自启的服务并启动（服务端对局域网开放，本机客户端只给自己用）
#   tools/service-mac.sh start       不写开机自启，只在后台运行到重启/注销为止
#                                    （~/Library/LaunchAgents 不可写时 install 会自动改用这种方式）
#   tools/service-mac.sh status      查看运行状态和发给同事的地址
#   tools/service-mac.sh restart     更新代码后重启
#   tools/service-mac.sh uninstall   停止并移除（数据保留在 data/ 和 ~/.perfscope）
# 服务端运行期间 Mac 不会因为闲置而休眠（caffeinate -i -s）；合上笔记本盖子仍会休眠。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
SRV=ai.plaud.perfscope.server
CLI=ai.plaud.perfscope.client
SERVER_PORT="${SERVER_PORT:-9000}"
CLIENT_PORT="${CLIENT_PORT:-8765}"
VPY="$ROOT/.venv/bin/python"
DOMAIN="gui/$(id -u)"

lan_ip() { ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1"; }

ensure_env() {
  if [ ! -x "$VPY" ]; then
    PY="${PYTHON:-python3}"
    command -v "$PY" >/dev/null || { echo "没有找到 Python 3（brew install python@3.12）"; exit 1; }
    echo "创建虚拟环境…"; "$PY" -m venv "$ROOT/.venv"
  fi
  echo "检查依赖…"
  "$VPY" -m pip install -q --disable-pip-version-check -r "$ROOT/server/requirements.txt" -r "$ROOT/client/requirements.txt"
  mkdir -p "$ROOT/data"
}

# label workdir log -- program args...
write_plist() {
  local label=$1 workdir=$2 log=$3; shift 3
  "$VPY" - "$LA/$label.plist" "$label" "$workdir" "$log" "$@" <<'EOF'
import os, plistlib, sys
path, label, workdir, log, *args = sys.argv[1:]
env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"}
env.update(dict(kv.split("=", 1) for kv in os.environ.get("PS_ENV", "").split("\n") if "=" in kv))
with open(path, "wb") as f:
    plistlib.dump({"Label": label, "ProgramArguments": args, "WorkingDirectory": workdir,
                   "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
                   "StandardOutPath": log, "StandardErrorPath": log, "ProcessType": "Interactive"}, f)
EOF
}

load() { launchctl bootout "$DOMAIN/$1" 2>/dev/null || true; launchctl bootstrap "$DOMAIN" "$LA/$1.plist"; }

# launchd jobs without a plist file: kept alive and restarted by launchd until logout/reboot
submit_all() {
  for l in $SRV $CLI; do launchctl remove "$l" 2>/dev/null || true; done
  sleep 1
  launchctl submit -l $SRV -o "$ROOT/data/server.log" -e "$ROOT/data/server.log" -- \
    /usr/bin/caffeinate -i -s /usr/bin/env "DATABASE_URL=sqlite:///$ROOT/data/perfscope.db" "BLOB_DIR=$ROOT/data/blobs" \
    /bin/sh -c "cd '$ROOT/server' && exec '$VPY' -m uvicorn perfserver.main:app --host 0.0.0.0 --port $SERVER_PORT --log-level warning"
  launchctl submit -l $CLI -o "$ROOT/data/client.log" -e "$ROOT/data/client.log" -- \
    /usr/bin/env "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" "PERFSCOPE_SERVER_URL=http://127.0.0.1:$SERVER_PORT" \
    /bin/sh -c "cd '$ROOT/client' && exec '$VPY' -m perfclient serve --port $CLIENT_PORT --no-browser"
}

wait_up() { for i in $(seq 1 30); do curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$CLIENT_PORT/" && break; sleep 1; done; }

status() {
  local ip; ip=$(lan_ip)
  for l in $SRV $CLI; do
    if launchctl list "$l" >/dev/null 2>&1 && [ ! -f "$LA/$l.plist" ]; then
      echo "$l: 后台运行中（重启电脑后需要运行 $0 start）"
    elif launchctl print "$DOMAIN/$l" >/dev/null 2>&1; then
      state=$(launchctl print "$DOMAIN/$l" | awk -F'= ' '/^\tstate/{print $2; exit}')
      echo "$l: $state"
    else
      echo "$l: 未安装"
    fi
  done
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$SERVER_PORT/api/me"; then
    echo
    echo "发给同事的地址：http://$ip:$SERVER_PORT （打开后点「下载客户端」）"
    echo "你自己的客户端：http://127.0.0.1:$CLIENT_PORT"
  else
    echo "服务端暂时没有响应，日志：$ROOT/data/server.log"
  fi
}

case "${1:-status}" in
  install)
    ensure_env
    if ! { mkdir -p "$LA" 2>/dev/null && [ -w "$LA" ]; }; then
      echo "$LA 不可写（通常是公司电脑的管理策略），改为后台运行：重启电脑后需要再运行一次 $0 start"
      submit_all; wait_up; status; exit 0
    fi
    PS_ENV="DATABASE_URL=sqlite:///$ROOT/data/perfscope.db
BLOB_DIR=$ROOT/data/blobs" \
      write_plist $SRV "$ROOT/server" "$ROOT/data/server.log" \
      /usr/bin/caffeinate -i -s "$VPY" -m uvicorn perfserver.main:app --host 0.0.0.0 --port "$SERVER_PORT" --log-level warning
    PS_ENV="PERFSCOPE_SERVER_URL=http://127.0.0.1:$SERVER_PORT" \
      write_plist $CLI "$ROOT/client" "$ROOT/data/client.log" \
      "$VPY" -m perfclient serve --port "$CLIENT_PORT" --no-browser
    load $SRV; load $CLI
    wait_up; status ;;
  start) ensure_env; submit_all; wait_up; status ;;
  stop) for l in $SRV $CLI; do launchctl remove "$l" 2>/dev/null || true; launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; done; echo "已停止" ;;
  restart)
    if [ -f "$LA/$SRV.plist" ]; then launchctl kickstart -k "$DOMAIN/$SRV"; launchctl kickstart -k "$DOMAIN/$CLI"; else submit_all; fi
    wait_up; status ;;
  uninstall)
    for l in $SRV $CLI; do launchctl remove "$l" 2>/dev/null || true; launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; rm -f "$LA/$l.plist" 2>/dev/null || true; done
    echo "已停止并移除后台服务。数据仍在 $ROOT/data 和 ~/.perfscope" ;;
  status) status ;;
  *) echo "用法：$0 install | start | stop | status | restart | uninstall"; exit 1 ;;
esac
