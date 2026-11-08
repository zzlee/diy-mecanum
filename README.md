# diy-mecanum — TurboPi 機器人 bring-up 工作區

树莓派 5 + Hiwonder TurboPi（麥克納姆四輪底盤 + ROS 擴充板）的**實作紀錄、診斷工具與自製監控面板**。
內容以**實測證據**為主：每個結論都附可重現指令，凡是推論而未經查證的部分會在文件中明標。

文件語言以繁體中文為主。

## 硬體與軟體環境

| 項目 | 內容 |
| :-- | :-- |
| 主機 | Raspberry Pi 5（8 GB），SD 卡 29.7 GiB |
| 底盤 | Hiwonder TurboPi，麥克納姆四輪，無輪編碼器回讀（純開環） |
| 主控板 | Hiwonder ROS 擴充板，經 UART `/dev/rrc`（`ttyAMA0` @ **1,000,000 baud**） |
| ROS | ROS 2 Humble，跑在 `--net=host`、`privileged` 的 Docker 容器 `TurboPi` 內 |
| 感測 | 單目相機（`32e6:9005`，640×480 YUYV 30fps，已標定）+ 單一**前視**超音波 |
| 供電 | 18650 ×2（3.7 V / 2200 mAh），`battery` topic 回報 2S 總電壓（mV） |

## 值得先看几個結論

- **WiFi AP 從頭就沒壞**：`WRONG_KEY` 的根因是 `brcmfmac` AP 模式只廣播 WPA1/TKIP（`RSN-FLAGS (none)`）的相容性問題，改 `rsn/ccmp` 後一次連線成功。詳 `HANDOVER-2026-11-08.md` §3。
- **`imu_raw` 無資料屬軟体不可修**：`enable_reception()` 只設定 client 端旗標、不下 UART 命令；`PACKET_FUNC_IMU = 7` 是 MCU→host 的**回報**方向。同條件對照實驗下 `battery` 收到 248 筆、`imu_raw` 0 筆 ⇒ MCU 從未發出 IMU 封包。詳 `SENSORS.md`。
- **厂家 vendor bug 修復**：`get_pwm_servo_state` 回調簽名與欄位型別錯誤（`uint8` 純量卻被當成陣列迭代），已修復並獨立驗證（`work/pwm-servo-fix/`）。
- **馬達指令的單位其實是「占空比 %」而不是 m/s**，且角速度項未除以輪半徑、`max_angular_speed` 宣告後從未被使用。鏈路與極性映射經線上監聽查證。
- **啟動門檻（地磚實測）**：前進 20%、後退 18% 占空比；低於此只會聽到馬達聲而**零位移**，且因為**無輪速回讀**，軟体完全看不見「有命令但沒動」。
- **`angular.z` 方向實測：厂家的角速度**符合** ROS REP-103**（`+1.44 rad/s` → 車頭往【左】，影像位移 `dx=-36.5 px`、信心 91）。
  ⚠️ 本項曾一度誤判為「反向」，原因是**影像位移腳本的符號解讀寫反了且未做单測**；已補上以已知位移校準的 `selftest()`，並更正本節。詳 `work/drive-test/shift_detect.py` 與 `HANDOVER` §14.6。

## 目錄

```
AGENTS.md                      多代理分工規範（雲端協調者 + 地端執行者）
HANDOVER-2026-11-08.md         當日完整交接紀錄（含所有實測數據與踩坑）
SENSORS.md                     感測器/馬達盤點、可擴展資源、開發結論
webui/                         即時監控面板 + **带看門狗的驅動閘道**（Node 24 零依賴）
  server.mjs                     rosbridge + SSH 遙測 + 影像代理；伺服端安全護欄
  index.html                     單一頁面儀表板
work/pwm-servo-fix/            vendor bug 修復的 brief / 脈絡 / patch / 回報
work/drive-test/               驅動測試工具：門檻階梯、逐輪測試、影像位移判讀、topic 監聽
work/power-check/              供電安檢清單 + 電池放電記錄器（9 Hz CSV）
```

## `webui/` 使用方式

```bash
cd webui && node server.mjs          # 預設綁 127.0.0.1:8787
# 開啟 http://localhost:8787
```

需要的端點：Pi 上的 rosbridge `:9090`、`web_video_server` `:8080`，以及到 Pi 的免密 SSH（`PI_HOST` 環境變數可覆位址）。

**驅動是預設鎖定、且護欄在伺服端**（不是只在瀏覽器）：

| 護欄 | 行為 |
| :-- | :-- |
| 未武裝不下發 | `/api/motion/beat` 未 arm 一律 409 |
| 數值夾限 | 超出 `vx/vy/wz` 上限回 400（不是靜音挾收） |
| 心跳看門狗 | 用戶端逾時 → 自動歸零並在伺服端留原因 |
| 心跳遺失即停機 | 用戶端每 220 ms 心跳，斷線約 1 s 內歸零（瀏覽器是分頁會自己死掉的元件，所以 watchdog 必須在 server） |
| 發完歸零 | 每次動作結束後補發零速並解除武裝 |
| 佔線預檢 | 時會先偵測是否有其它節點正在搶 `/cmd_vel` |
| 急停 latching | 需人工明確點選才能解除 |

⚠️ 實車測試前請務必：輪子懸空或前後設障礙、先低占空比、確認看門狗生效。**任何驅動都可能造成財物或人員損害，本倉庫工具不提供任何安全保證。**

## 關於佔位符與未附之檔案

公開倉庫刻意做了兩類處理：

1. **佔位符**：`<AP-PSK>`（Pi AP 的 WiFi 密碼，8 字元厂家預設值）、`<GPU_HOST_USER>@<GPU_HOST>`（第三方 GPU 主機帳號與位址）。實際值存放在已被 `.gitignore` 排除的 `SECRETS.local.md`。
2. **未附檔案**：厂家專有源碼的**全文副本**未入庫（僅保留 `fix.patch` 差異與修復報告）；相機快照 `work/drive-test/frames/*.jpg` 亦未入庫。

## 與厂家的關係

本倉庫與 Hiwonder / MentorPi 無任何關係，亦未獲得其授權背書。所有对厂家固件與 ROS 節點的行为描述，皆來自本機對其所交付檔案的閱讀與實測。
