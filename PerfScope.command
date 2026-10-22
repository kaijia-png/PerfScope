#!/usr/bin/env bash
# macOS：双击启动 PerfScope（会打开一个终端窗口，关掉这个窗口就停止）
cd "$(dirname "$0")"
./start.sh "$@"
code=$?
if [ $code -ne 0 ]; then
  echo
  read -n 1 -s -r -p "启动没有成功，看完上面的提示后按任意键关闭窗口"
  echo
fi
exit $code
