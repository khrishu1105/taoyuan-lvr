#!/bin/bash
# 雙擊此檔即可啟動本機伺服器並開啟桃園預售屋實登查詢頁
cd "$(dirname "$0")"
PORT=8777
# 若 port 已被占用就換一個
if lsof -i :$PORT >/dev/null 2>&1; then PORT=8778; fi
echo "啟動本機伺服器 http://localhost:$PORT ..."
python3 -m http.server $PORT >/tmp/lvr_server.log 2>&1 &
SVPID=$!
sleep 1
open "http://localhost:$PORT/index.html"
echo ""
echo "查詢頁已開啟。關閉此視窗前請按 Ctrl+C 停止伺服器。"
echo "（伺服器 PID: $SVPID）"
wait $SVPID
