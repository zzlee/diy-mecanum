# pwm_servo/get_state 修復回報

**最終狀態：SUCCESS**（修復迴圈使用 2/5 次；未動馬達/舵機、未發 `cmd_vel`、未做任何 git 操作、未 build）

## 1. 修改的檔案與行號（容器 `TurboPi` 內）

`/home/ubuntu/ros2_ws/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_node.py`
函式 `get_pwm_servo_state`，原檔 **156–169 行**（改後 156–176 行）。同檔其他函式 0 更動。

* 備份（保留、未刪）：`...ros_robot_controller_node.py.bak-20261108`，md5 `aa25459b0cf08f0566032b7194281116`
* 部署後 md5：`885c9822cae50e21fb40453f8a899a3f`（與本目錄 `ros_robot_controller_node.fixed.py` 一致）
* 部署方式：`docker exec -i TurboPi bash -c 'cat > <src 檔>'`（寫穿同一 inode `787684`；
  `build/ros_robot_controller/ros_robot_controller` → `src/...` 的 symlink 完好，`links=1 inode 相同`）
* 未执行任何 `colcon build`。`python3 -m py_compile` OK（py 3.10）。

## 2. Root cause（一句話 + 三層）

一句話：`get_pwm_servo_state` 是 ROS 1 寫法的回調，在 rclpy 下從簽名到回傳契約都不合，異常直接從 executor 冒出把進程打死。

實測到的三層（前兩層不修都還是會死）：

1. **簽名**：rclpy 以 `(request, response)` 呼叫 → `TypeError: takes 2 positional arguments but 3 were given`（COORDINATOR 已記錄的症狀）。
2. **回傳契約**（CONTEXT 未涵蓋，實測撞出）：本機 rclpy 的 executor 是
   `response = await await_or_execute(srv.callback, request, srv.srv_type.Response()); srv.send_response(response, header)`
   （`/opt/ros/humble/local/lib/python3.10/dist-packages/rclpy/executors.py:396-402`），
   `send_response()` 第一件事就是 `if not isinstance(response, self.srv_type.Response): raise TypeError()`
   （`rclpy/service.py:72-73`）。所以 **回調必須 `return response`**；只寫 `response.state = states`（回傳 `None`）
   照样死——14:37:38 實測 `pid 10820 exit code 1`，traceback 收在 `srv.send_response → raise TypeError()`。
   ⚠️ 同檔 `:216` 的 `get_bus_servo_state`（CONTEXT 的「範本」）**本身就是這個 bug**，见 §5。
3. **SDK 回傳純量 vs. msg 是陣列欄位**：`pwm_servo_read_position/offset` 用 `struct` 只回一個 int，
   而 `PWMServoState.position` 是 `uint16[]`、`offset` 是 `int16[]`；直接 `data.position = state` 擲
   `AssertionError: The 'position' field must be a set or sequence ...`（容器內實測）。故改成 `[state]`。
   （實證：修好後實機回傳 `position=[1500], offset=[0]`。）

另把 `response.success = True` 補上（faithful port 自 ROS 1 的 `return [True, states]`；`GetPWMServoState.srv`
的 response 有 `bool success`）。

## 3. 部署/重啟流程（實際可行版本）

```bash
SRC=/home/ubuntu/ros2_ws/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_node.py
ssh -i ~/.ssh/id_ed25519 pi@192.168.149.1 "sudo docker exec TurboPi cp $SRC $SRC.bak-20261108"
ssh -i ~/.ssh/id_ed25519 pi@192.168.149.1 "sudo docker exec -i TurboPi /bin/bash -c 'cat > $SRC'" < ros_robot_controller_node.fixed.py
ssh -i ~/.ssh/id_ed25519 pi@192.168.149.1 "sudo docker exec TurboPi python3 -m py_compile $SRC"
# 見 §5：systemctl restart 不會殺掉容器內的舊 stack，必須先清乾淨
sudo systemctl stop start_node
sudo docker exec TurboPi /bin/bash -c 'kill -INT $(pgrep -f "ros2 launch bringup bringup.launch.py|ros2 launch rosbridge_server|ros2 run web_video_server|install/[a-z_]+/lib/.*--ros-args|usb_cam_node_exe|lib/web_video_server/web_video_server")'
sleep 20    # 確認 ps 裡已無 ros 進程、8080/9090 已釋放
sudo systemctl start start_node   # 等 ~90 s
```

## 4. 驗證輸出摘要

| 項目 | 結果 |
|---|---|
| `ros2 node list \| grep -x /ros_robot_controller` | `/ros_robot_controller`（僅 1 份，無重名） |
| `journalctl -b -u start_node --since 14:41:13 \| grep -cE 'has died\|Traceback'` | **0** |
| 全開機計數（對照用） | 34（皆為修復前的崩潰與雙 stack 衝突，時間戳都在 14:41:13 之前，無一筆是 `ros_robot_controller`） |
| `battery` 呼叫後 | `data: 4518`；`ros2 topic hz` ≈ **9.1 Hz**（呼叫前後皆正常） |

Service 實際回傳（讀取型，無任何 `set_*`/`cmd_vel`）：

```
# flags 全 0（純軟體路徑）
GetPWMServoState_Response(success=True, state=[PWMServoState(id=[], position=[], offset=[])])

# get_position=1 get_offset=1（實機唯讀）
GetPWMServoState_Response(success=True, state=[PWMServoState(id=[], position=[1500], offset=[0])])

# 連續 3 次、單次-request-兩顆舵機（id1 讀 position+offset，id2 只讀 position），三次完全一致
GetPWMServoState_Response(success=True, state=[PWMServoState(id=[], position=[1500], offset=[0]),
                                               PWMServoState(id=[], position=[1500], offset=[])])
```

離線實證（不碰硬體、不建節點）：把部署後的函式拿 fake `board`（回傳純量）跑，
`isinstance(ret, Response) = True`（flags=0 / flags=1 皆過），`position=[512] offset=[-3]`。
磁碟：`/` 92%（2.3G 可用），容器 `/tmp` 未留檔（harness 已 `rm`）。

## 5. 給協調者的三個附加發現

1. 🔴 **`systemctl restart start_node` 不會停掉容器內的 ROS stack**：unit 跑 `docker exec`，stop 只殺 host 端的
   `docker` client，容器內 `ros2 launch`（ppid 0）變孤兒繼續跑 → 出現**兩套 stack**：節點重名、
   `/dev/rrc` 被兩個 controller 同開（`SerialException: device reports readiness to read but returned no data
   (device disconnected or multiple access on port?)` → 新節點的 `recv_task` 執行緒直接死）、
   `web_video_server` 8080 / `rosbridge` 9090 `Address already in use` 而死。
   **restart 後必須先清容器內殘留進程、再 start**，否則 service 呼叫可能打到舊（buggy）節點，驗證結果不可信。
2. 🔴 **同檔 `:216 get_bus_servo_state`（以及 `:196 get_bus_servo_state` 家族）仍是「只填 response 不 return」**：
   照 rclpy 的 `_execute_service`，呼叫 `/ros_robot_controller/bus_servo/get_state` 應該同樣會打死節點。
   本次範疇限定「只改 `get_pwm_servo_state`」，故**未修**，建議另開任務。
3. 🟡 `pwm_servo_read_*` 底層是 `queue.get(block=True)`（無 timeout）：若 RRC 單晶片不回檔，service 回調會
   永久卡住單執行緒 executor（節點變無回應）。本次實測有正常回檔，未觸發。
4. 🟡 回傳的 `PWMServoState.id` 恆為空（原碼也沒填），維持原樣未動。

## 6. 回滾指令（具體、實測可用的寫穿 inode 版本）

```bash
SRC=/home/ubuntu/ros2_ws/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_node.py
ssh -i ~/.ssh/id_ed25519 pi@192.168.149.1 "sudo docker exec TurboPi cp $SRC.bak-20261108 $SRC; \
  sudo docker exec TurboPi md5sum $SRC"        # 應為 aa25459b0cf08f0566032b7194281116

# 乾淨重啟（先 stop → 清容器內殘留 → start）
ssh -i ~/.ssh/id_ed25519 pi@192.168.149.1 "sudo systemctl stop start_node; \
  sudo docker exec TurboPi /bin/bash -c 'kill -INT \$(pgrep -f \"ros2 launch bringup bringup.launch.py|ros2 launch rosbridge_server|ros2 run web_video_server|install/[a-z_]+/lib/.*--ros-args|usb_cam_node_exe|lib/web_video_server/web_video_server\")'; \
  sleep 20; sudo systemctl start start_node"
sleep 90
```

（`cp` 不加 `-a/--remove-destination` 會就地截斷寫入同一 inode，`build/` 的 symlink 不會被破壞。）

## 7. 本工作目錄交付物

* `fix.patch` — unified diff（`patch -p1 --dry-run` 對 `ros_robot_controller_node.py` 通過，套完 md5 == 部署版）
* `ros_robot_controller_node.fixed.py` — 修好整份（md5 `885c9822cae50e21fb40453f8a899a3f`）
* `REPORT.md` — 本檔
