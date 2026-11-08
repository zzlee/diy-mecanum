# CONTEXT — 修 `get_pwm_servo_state` vendor bug

這支檔案是**座標與事實清單**，派工前已由協調者逐項實測確認。worker 不需要重新探索，直接照做並驗證。

## 目標（一句話）
修好容器內 `ros_robot_controller_node.py` 的 `get_pwm_servo_state`，讓 `/ros_robot_controller/pwm_servo/get_state` 這個 service 可以被安全呼叫而**不會殺死節點**。

## 現況與根因（已證實）
- 節點在 `:48` 把它註冊成 **service**：
  `self.create_service(GetPWMServoState, '~/pwm_servo/get_state', self.get_pwm_servo_state)`
- 但 `:156` 的簽名是 **ROS 1 寫法**，而且**回傳值也是 ROS 1 寫法**：
  ```python
  def get_pwm_servo_state(self, msg):        # ← 錯：rclpy 會以 (request, response) 呼叫
      states = []
      for i in msg.cmd:                      # ← 錯：应该是 request.cmd
          data = PWMServoState()
          if i.get_position: ...
          if i.get_offset:   ...
          states.append(data)
      return [True, states]                  # ← 錯：ROS 2 要填 response 並回傳 None
  ```
- 實測後果：呼叫該 service → `TypeError: RosRobotController.get_pwm_servo_state() takes 2 positional arguments but 3 were given` → `process has died [pid 745, exit code 1]` → 馬達/伺服/電池/按鈕/OLED/蜂鳴器**全部失能**（只能靠重啟救回）。
- **同檔的正確範本**在 `:216`（同一個 package、同樣是 service 的 `bus_servo/get_state`）：
  ```python
  def get_bus_servo_state(self, request, response):
      states = []
      for i in request.cmd:
          ...
          states.append(data)
      response.state = states                # ← 結尾是這樣，沒有 return
  ```

## 要改的確切內容
1. `def get_pwm_servo_state(self, msg):` → `def get_pwm_servo_state(self, request, response):`
2. `for i in msg.cmd:` → `for i in request.cmd:`
3. 結尾 `return [True, states]` → `response.state = states`
   - ⚠️ **務必先讀 `.srv` 確認 response 欄位的真實名稱與型別**（bus 版是 `response.state`，pwm 版理論上相同，但要以檔案為準，不要照抄）：
     `sudo docker exec -u ubuntu TurboPi /bin/zsh -c 'find /home/ubuntu/ros2_ws -name "GetPWMServoState.srv" | head -2; ... cat 之'`
     以及 `... find /home/ubuntu/ros2_ws -name "PWMServoState.msg" | head; cat`
4. **只改這一個函式**。同檔其他 `set_*` 回調收 `msg` 是**正確的**（它們是 subscription），不要一併改。

## 部署與生效方式（關鍵，做錯會出問題）
- 目標檔（容器內）：`/home/ubuntu/ros2_ws/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_node.py`
- 本 workspace 已有一份拷貝：`./ros_robot_controller_node.py`（353 行，md5 `aa25459b0cf08f0566032b7194281116`）
- **該 workspace 是用 `colcon build --symlink-install` 建的**：實測 `python3 -c "import ros_robot_controller.ros_robot_controller_node as m; print(m.__file__)"` → `/home/ubuntu/ros2_ws/build/ros_robot_controller/ros_robot_controller/ros_robot_controller_node.py`，那是**指回 `src/` 的 symlink**。
  - ⇒ **改 `src/` 裡的檔案就會在下一次節點啟動時直接生效，完全不需要 build。**
  - 🔴 **禁止執行未帶 `--symlink-install` 的 `colcon build`**：會把 symlink 變成實體 copy，既改變環境又多吃磁碟（根分割區已 93%，剩 2.2G）。原則上**不需要 build**。
  - 🔴 **避免用 `docker cp` 把檔案丟進那個路徑**：可能會把 symlink 換成實體檔，之後改 `src/` 就不生效了。請用「寫穿 symlink」的方式，例如
    `sudo docker exec -i TurboPi bash -c 'cat > /home/ubuntu/ros2_ws/src/.../ros_robot_controller_node.py' < 本地檔`
- 重啟方式（**不要用 reboot**）：在 **Pi 主機**上 `sudo systemctl restart start_node`
  （unit: `Restart=no`、`ExecStart=/bin/bash /home/pi/turbopi/start_node.sh`，內容是 `docker exec -u ubuntu -w /home/ubuntu TurboPi /bin/zsh -c "source ~/.zshrc && ros2 launch bringup bringup.launch.py"`）。整堆 ROS 約 60–90 秒回來。

## 驗證步驟（缺一不可）
1. 改檔前先備份：`sudo docker exec TurboPi cp -a <檔> <檔>.bak-20261108`
2. 語法檢查：`python3 -m py_compile <檔>`（容器內跑，用同版 python3.10）
3. `sudo systemctl restart start_node`，等 90 秒
4. 節點回來：`ros2 node list` 含 `/ros_robot_controller`
5. **讀取型 service 呼叫**（先照 `.srv` 組出正確的 request，不要亂填）：
   `ros2 service call /ros_robot_controller/pwm_servo/get_state ros_robot_controller_msgs/srv/GetPWMServoState "{cmd: [{id: 1, get_position: true, get_offset: true}]}"`
   （欄位名稱以 `.srv`/`.msg` 為準；`id` 之類若型別不符會報錯，那是你的組裝問題，不是 bug）
6. **節點存活才算過**：
   - `ros2 node list | grep -x /ros_robot_controller` 有輸出
   - `sudo journalctl -b -u start_node --no-pager | grep -cE "has died|Traceback"` == `0`
   - 呼叫之後 `/ros_robot_controller/battery` 仍在出資料（`ros2 topic echo --once`）
7. 失敗就回滾：把 `.bak-20261108` 蓋回去、重啟、回報 root cause。**修復迴圈上限 5 次**。

## 存取方式（都免密）
- `ssh -i ~/.ssh/id_ed25519 pi@192.168.149.1`（這是機器人 AP 那條線，`sudo -n` 免密可用）
- 容器內執行：`sudo docker exec -u ubuntu -w /home/ubuntu TurboPi /bin/zsh -c 'source ~/.zshrc >/dev/null 2>&1; <cmd>'`
- Pi **沒有 internet**（AP 模式），所以不要 apt/pip install，也不要抓任何東西

## 紅線（違反即中止並回報）
1. **只能改這一個函式**；不碰 Wi-Fi / NetworkManager / systemd unit 內容 / 其他 package
2. **嚴格禁止 git 操作**（`/home/ubuntu/ros2_ws/src/driver` 是 git work tree 且已有其他人為變更，不要 commit/checkout/reset/clean）
3. **不得驅動機器人**：不發 `cmd_vel`、不呼叫任何 `set_*` service/topic、不讓馬達或舵機動。只做讀取型呼叫
4. 不 `rm -rf`、不 `git clean`、不刪 `.bak-*` 備份
5. 磁碟 93%，**不要在容器裡產生大檔**（build log 導到 `/tmp` 並在結束前清掉）

## 交付物（請在工作目錄留下，並在回報裡摘要）
- `fix.patch` — 針對 `./ros_robot_controller_node.py` 的 unified diff
- `ros_robot_controller_node.fixed.py` — 修好的整份檔案
- 回報（Markdown）：
  - 建置/部署最終狀態（SUCCESS / FAILED / BLOCKED）
  - 修改的檔案與行號
  - root cause 一句話
  - 驗證輸出摘要：service 回傳內容、`grep -c "has died"` 數字、battery 是否仍有資料
  - 回滾方式（具體指令）
