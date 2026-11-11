#!/bin/sh
# 殺掉容器內所有 phone-rc 程式。
# ⚠️ 兩點都踩過：
#   1) regex 寫成 phonerc/app.py 會比对本殼層自己的 cmdline（先殺到自己）⇒ 用 [p] 開頭
#   2) rclpy 會攔截 SIGTERM 但不會因此退出 ⇒ 必須 TERM 之後補 KILL
pat='[p]honerc/app.py'
pids=$(ps -eo pid,args | grep "$pat" | awk '{print $1}')
[ -z "$pids" ] && { echo "cleanup: 本來就沒有程序"; exit 0; }
for p in $pids; do kill -TERM "$p" 2>/dev/null; done
sleep 2
for p in $pids; do
  if kill -0 "$p" 2>/dev/null; then kill -KILL "$p" 2>/dev/null; fi
done
sleep 1
n=$(ps -eo args | grep -c "$pat")
echo "cleanup: 已清理 $(echo "$pids" | wc -w) 個，殘留 $n 個"
[ "$n" -eq 0 ] || exit 1
exit 0
