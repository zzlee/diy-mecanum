#!/bin/bash
# 在 Raspberry Pi 的控制台（鍵盤滑鼠那個 terminal）執行：
#   sudo bash pi-console-diag-1.sh
# 只做診斷與回報，不修改任何設定、不驅動任何馬達。

echo "########## A) 開機驗證報告（opencode 埋的那支腳本輸出）##########"
if [ -f /home/pi/ap-boot-verify.txt ]; then
  sed -n '/KEY FACTS/,/END KEY FACTS/p' /home/pi/ap-boot-verify.txt
else
  echo "(ap-boot-verify.txt 不存在)"
  ls -l /home/pi/*.txt 2>&1 | head -5
fi
echo "--- 那個一次性服務註冊了嗎 / 跑過沒 ---"
systemctl list-unit-files 'ap-boot-verify*' --no-pager 2>&1 | head -5
systemctl status ap-boot-verify.service --no-pager 2>&1 | head -8

echo
echo "########## B) 到底是誰在廣播 HW-6720577E ##########"
echo "--- hostapd 行程"
pgrep -af hostapd || echo "    (沒有 hostapd 行程 → AP 由 wpa_supplicant/NM 自己起)"
echo "--- 相關 service 狀態"
for u in wifi wifi.service.disabled NetworkManager hostapd start_node hw_button_scan; do
  printf "  %-22s " "$u"; systemctl is-enabled "$u" 2>&1 | tr -d '\n'; printf " / "; systemctl is-active "$u" 2>&1
done
echo "--- wifi.py 是否又被拉起"
pgrep -af 'hiwonder-toolbox/wifi.py' || echo "    wifi.py 不在 (OK)"

echo
echo "--- 目前作用中的連線 / wlan0 實際角色"
nmcli -t -f NAME,DEVICE,TYPE,STATE con show --active
iw dev wlan0 info

echo
echo "--- NM AP profile 生效設定（密碼只顯示長度，不外洩）---"
for f in /etc/NetworkManager/system-connections/*.nmconnection; do
  [ -f "$f" ] || continue
  echo "[$f]"
  grep -E '^\[|^ssid|^mode|^band|^channel|^key-mgmt|^proto|^pairwise|^group|^auth-alg|^address4|^method|^autoconnect|^priority' "$f"
  awk -F= '/^psk=/{printf "  psk: len=%d trailing_ws=%s\n", length($2), (match($2,/[ \t\r]+$/)?"YES":"no")}' "$f"
  grep -E '^psk-flags' "$f" || echo "  psk-flags: (未設定)"
  stat -c '  檔案修改時間: %y' "$f"
done

echo
echo "########## C) ROS 節點是否自動回來 ##########"
timeout 25 docker exec -u ubuntu -w /home/ubuntu TurboPi /bin/zsh -c \
  'source ~/.zshrc >/dev/null 2>&1; ros2 node list' 2>&1 | sort | head -20
echo "--- /ros_robot_controller 是否存在 ---"
timeout 25 docker exec -u ubuntu -w /home/ubuntu TurboPi /bin/zsh -c \
  'source ~/.zshrc >/dev/null 2>&1; ros2 node list' 2>&1 | grep -x '/ros_robot_controller' \
  && echo "    OK" || echo "    仍缺"

echo
echo "########## D) 其他 ##########"
echo "--- 監聽埠"; ss -lnt 2>/dev/null | grep -E ':(22|8080|9090|9026|5900)\b'
echo "--- 根分割區"; df -h / | tail -1
echo "--- 開機期間 wifi/NetworkManager 相關錯誤"
journalctl -b --no-pager 2>/dev/null | grep -iE 'wifi|hostapd|dnsmasq|wpa_supp' | grep -iE 'fail|error|deni|refus' | tail -8
echo "########## DIAG END ##########"
