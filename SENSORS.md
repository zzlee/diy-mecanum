# TurboPi (Raspberry Pi 5) 感測器 / 致動器盤點

> 狀態分級依據「**本輪是否有實測證據**」，不是依據 ROS 清單上有沒有這個 topic。
> 實測時間：2026-11-08。證據來源標在每項後面。
> 主控板：Hiwonder ROS 擴充板，經 UART `/dev/rrc`（`ttyAMA0` @ **1,000,000 baud**）連到 Pi。ROS 2 Humble 跑在 `--net=host`、`privileged` 的 Docker 容器 `TurboPi` 內。

---

## 🟢 可用（實測有資料）

| 裝置 | 介面 / topic | 實測值 | 證據 |
| :-- | :-- | :-- | :-- |
| **USB 相機** `32e6:9005` icSpring | `/image_raw`(Image)、`/image_raw/compressed`、`/camera_info` | 640×480 YUYV 30fps；內參 `fx=473.45 fy=474.22` | rosbridge 訂閱 3–4 筆/7s；`GET :8080/snapshot?topic=/image_raw` → 200、105 KB JPEG |
| **超音波測距**（I2C-1 `0x77`，模組附 RGB） | `/sonar_controller/get_distance`（`std_msgs/Int32`，topic 不是 service） | 255–343 mm；`5000` = 超量程/無回波 | rosbridge 訂閱 4 筆/8s；`ros2 topic echo --once` = 343 |
| **電池電壓（2S 總壓）** | `/ros_robot_controller/battery`（`UInt16`，**單位 mV**） | 充飽裝回後實測 **7828–7870 mV**（≈3.93 V/cell，約 60–70%）；約 9.1 Hz 出料 | 電池供電下連讀 5 次；判別規則見 `HANDOVER §12` |
| **PWM 舵機 ×2**（雲台：ID1 左右、ID2 上下） | `/ros_robot_controller/pwm_servo/get_state` | ID1 `position=[1500] offset=[0]`；ID2 `offset=[10]` | 連續 3 次呼叫一致（**修 vendor bug 後才可讀**，見 `HANDOVER §8`） |
| **驅動馬達 ×4**（麥克納姆，ID 1–4） | `/cmd_vel`(Twist) → `/ros_robot_controller/set_motor_speeds`（`MotorsSpeedControl`） | 幾何：`wheelbase=0.1368`、`track_width=0.1410`、`wheel_diameter=0.065`；上限 1.0 m/s、1.0 rad/s | `mecanum.py` 源碼 + topic 存在；**未通電驅動**（紅線） |

## 🟡 有控制界面，但無回讀 / 未實測

| 裝置 | 界面 | 備註 |
| :-- | :-- | :-- |
| OLED 螢幕 | `set_oled`（`OLEDState`）、另有 `set_oled_text` | 只做命令下發，無狀態回報 |
| 蜂鳴器 | `set_buzzer`（`BuzzerState`） | 同上 |
| 機身 LED / WS2812 RGB | `set_led`、`set_rgb`、`sonar_controller/set_rgb` | 第三個是超音波板那顆 |
| 串列總線舵機（serial bus servo） | `bus_servo/get_state` / `set_state` / `set_position` | `cmd: []` 回 `success=True, state=[]`；**總線上有沒有掛東西未確認**。⚠️ 非空 `cmd` 有卡死 executor 風險（`queue.get(block=True)` 無 timeout，TODO-ab885143） |
| **8 通道 ADC `ADS7830`**（I2C-1 `0x48`） | **未接進 ROS** | 本輪以 command byte `0x84/0x85…` 讀過 raw：`0x90 0x5d 0xaf 0x3a 0xc3 0xd6 0xbd 0x01`。要用得自己寫節點 |

## ⚫ 名義上存在、實際拿不到資料

| 裝置 | 證據 | 判定 |
| :-- | :-- | :-- |
| **IMU（6 軸）** | `/ros_robot_controller/imu_raw` 有 publisher（RELIABLE/VOLATILE）但 **0 筆**；`ros2 topic hz` 25 秒 0；**手動搬動機器期間也 0 筆**；對照組同管線抓 `battery` = 248 行 | **MCU 從未送出 `PACKET_FUNC_IMU = 7` 封包**。`enable_reception()` 只是 client 端旗標（`self.enable_recv = enable`），SDK 無任何下行命令可要求回傳 ⇒ **軟體層無解**。UART RX 本身正常（電池封包一直進） |
| **按鍵** | `button` topic 0 筆；使用者回報主控板上看不到按鍵 | 此版本硬體很可能沒有按鍵 |
| **SBUS 遙控接收機** | `sbus` 0 筆 | 未接 / 未開機 |
| **搖桿** | `joy` 0 筆 | 未接 |

---

## ➕ 可擴充資源（Pi 5 側）

| 資源 | 狀態 |
| :-- | :-- |
| **I2C-1 匯流排** | 已佔用 `0x77`（超音波）、`0x48`（ADS7830）；**`0x68/0x69/0x6A` 全空** → 可直接加 MPU6050 / ICM20948 / BMI088 / LSM6DS 這類 6 軸 IMU |
| I2C-13 / I2C-14 | RP1 內部匯流排（全部位址都應答），**不是外部可用匯流排** |
| `SPI spidev10.0` | 可用 |
| GPIO | `GPIO16` 輸出 low、`GPIO26` 輸出 high（已被 `button_scan.py` 等佔用）；`GPIO13` 長按 3 秒 = 重置 WiFi、`GPIO23` 長按 3 秒 = `sudo halt` |
| 🔴 **`GPIO14/15` = UART0 = `/dev/rrc`** | **主控板命脈（1 Mbaud），擴充時不得佔用**。`dtoverlay=pi3-miniuart-bt` + `enable_uart=1` + `force_turbo=1` |
| USB | 有 1 個 USB WiFi（`MT7601U`）可當第二條 radio（AP + STA 並存用） |

---

## 對開發的直接結論

1. **能用的感測器只有兩個：相機 + 單點超音波。** 沒有 IMU、沒有輪速回讀、沒有第二個測距點。
2. **馬達是純開環**：`set_motor_speeds` 只發不收，`/cmd_vel` 沒有對應的 odom 回饋 topic。
3. ⇒ **目前無法建立可信的 odometry**。要做 SLAM / 閉環控制，外接 IMU（I2C，位址 `0x68`）+ 輪長或視覺里程計是必要條件，不是加分項。
4. **超音波只有一個、且只朝前**（`get_distance` 單點 Int32）：夠做前方障礙 Avoid，不夠做環場感知。
5. 視覺面可用：`usb_cam` + 已校正內參，且 `object_tracking` / `line_following` / `qrcode` / `gesture_control` / `avoidance` 五個廠視覺節點都在 graph 內（idle 狀態，未逐一驗功能）。
6. **待修/待補**：`bus_servo_read_*` 阻塞讀無 timeout（TODO-ab885143）；`PWMServoState.id` 恆空；ADS7830 未接進 ROS。

---

## 附：本次盤點用的指令（全部唯讀）

```bash
# 感測器即時有無出資料（注意 9090 是 WebSocket，不是裸 TCP；publish 訊息不含 id）
python3 - <<'PY'
import asyncio, json, websockets
async def probe(topic, typ, secs=7):
    async with websockets.connect("ws://192.168.149.1:9090", ping_interval=None, max_size=None) as ws:
        await ws.send(json.dumps({"op":"subscribe","topic":topic,"type":typ,"id":"p"})); n=0
        try:
            for _ in range(200):
                d=json.loads(await asyncio.wait_for(ws.recv(), timeout=secs))
                if d.get("op")=="publish" and d.get("topic")==topic:
                    n+=1
                    if n>=3: break
        except asyncio.TimeoutError: pass
        print(topic, n)
for t,y in [("/ros_robot_controller/battery","std_msgs/msg/UInt16"),
            ("/sonar_controller/get_distance","std_msgs/msg/Int32"),
            ("/ros_robot_controller/imu_raw","sensor_msgs/msg/Imu")]:
    asyncio.run(probe(t,y))
PY

# 相機
curl -s -o /tmp/s.jpg "http://192.168.149.1:8080/snapshot?topic=/image_raw"   # 只能用 base topic

# PWM 舵機狀態（id/get_position/get_offset 都是 uint8 純量，不是陣列）
ros2 service call /ros_robot_controller/pwm_servo/get_state \
  ros_robot_controller_msgs/srv/GetPWMServoState '{cmd: [{id: 1, get_position: 1, get_offset: 1}]}'
```
