{
  "id": "070dcfac",
  "title": "pwm_servo 讀回不核對回包 id（會讀到錯位值）",
  "tags": [
    "ros",
    "vendor-bug",
    "sdk"
  ],
  "status": "open",
  "created_at": "2026-11-08T08:26:11.768Z"
}

`ros_robot_controller_sdk.py:388-393` 的 `pwm_servo_read_and_unpack()`：
  data = self.pwm_servo_queue.get(block=True)     # 拿「下一個」，不核對回包 id／命令種類
  servo_id, cmd, info = struct.unpack(unpack, data)   # 回包裡的 servo_id 被丟棄

一次 get_state 對兩個 id 交錯做 pos/off 共 4 次讀取，pos 用 `<BBH`、off 用 `<BBb` 解不同長度封包 ⇒ 錯位時拿到不存在的值。
實證：命令 ID2 回 1500 後讀出 `position=[1565]`（從未命令過 1565）。

影響：任何依賴舵機位置讀回的閉環（視覺跟隨校準、位姿回饋）都不可靠。
修法（若要根治）：回包核對 servo_id 與 cmd，不符就丟棄續讀；並給 queue.get 加 timeout。
目前防護：webui 一律「單 id 單欄位 + 讀兩次取一致」，不一致回報「無法確認」。
相關：TODO-ab885143（同一族：阻塞讀取無 timeout）
