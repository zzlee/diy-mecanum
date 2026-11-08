#!/usr/bin/env node
/**
 * TurboPi 感測器即時儀表板（本機 Node.js，零依賴）
 *
 * 架構：瀏覽器 --SSE--> Node --WebSocket--> rosbridge(:9090) on Pi
 *                              |--SSH------> vcgencmd / systemd / df
 *                              |--HTTP-----> web_video_server(:8080) snapshot
 *
 * 為什麼零依賴：Node 24 內建全域 WebSocket client，HTTP 伺服器用 node:http，
 * 推送用 SSE（EventSource 自動重連），所以不需要 npm install。
 *
 * ⚠️ 安全設計：本 app **唯讀**。沒有任何 cmd_vel / set_* 服務呼叫。
 *    唯一會動到機器的是 /api/pwm_state（讀 PWM 舵機位置，已驗證安全）。
 *
 * ⚠️ 不訂閱 /image_raw：640x480 的 base64 JSON 會把 rosbridge 連線擠爆
 *    （本 session 實測過 keepalive ping timeout）。影像是走 :8080 snapshot。
 *
 * 用法：
 *   PI_HOST=192.168.149.1 PORT=8787 node server.mjs
 *   然後開 http://localhost:8787
 */
import http from "node:http";
import net from "node:net";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";

const PI_HOST = process.env.PI_HOST || "192.168.149.1";
const PI_SSH = process.env.PI_SSH || `pi@${PI_HOST}`;
const SSH_KEY = process.env.SSH_KEY || path.join(os.homedir(), ".ssh", "id_ed25519");
const PORT = Number(process.env.PORT || 8787);
const ROSBRIDGE = `ws://${PI_HOST}:9090`;
const SNAPSHOT_URL = `http://${PI_HOST}:8080/snapshot?topic=/image_raw`;
const HERE = path.dirname(fileURLToPath(import.meta.url));

// ---- 訂閱清單（全部唯讀感測器；刻意不含 /image_raw）----
// ⚠ throttle_rate：實測 `/sonar_controller/get_distance` 源頭以 **~605 Hz** 發布！
//   rosbridge 要逐條做 JSON 序列化，600 Hz 等於白耗 Pi 的 CPU。
//   儀表板 5 Hz 就夠用，所以用 rosbridge 協定的 throttle_rate 在源頭限流。
const TOPICS = [
  { topic: "/ros_robot_controller/battery", type: "std_msgs/msg/UInt16", label: "電池／電源軌", throttle_rate: 200, hint: "源頭 ≈9 Hz｜>6000 ⇒ 2S 電池 mV；≈4300-5700 ⇒ 5V 軌" },
  { topic: "/sonar_controller/get_distance", type: "std_msgs/msg/Int32", label: "超音波測距", throttle_rate: 200, hint: "源頭 ≈605 Hz（已限流至 5 Hz）｜mm；5000 = 超量程" },
  { topic: "/camera_info", type: "sensor_msgs/msg/CameraInfo", label: "相機內參", throttle_rate: 500, hint: "ROS 2 的 CameraInfo 欄位是小寫 k/d/p/r" },
  { topic: "/ros_robot_controller/imu_raw", type: "sensor_msgs/msg/Imu", label: "IMU 6 軸", throttle_rate: 200, hint: "已查證：MCU 從不送 func=7 ⇒ 無資料" },
  { topic: "/ros_robot_controller/button", type: "ros_robot_controller_msgs/msg/ButtonState", label: "按鍵", throttle_rate: 200, hint: "此版本硬體可能無按鍵" },
  { topic: "/ros_robot_controller/sbus", type: "ros_robot_controller_msgs/msg/Sbus", label: "SBUS 遥控", throttle_rate: 200, hint: "未接/未開機" },
  { topic: "/ros_robot_controller/joy", type: "sensor_msgs/msg/Joy", label: "搖桿", throttle_rate: 200, hint: "未接" },
];

// ---- 全域狀態 ----
const state = {
  ts: Date.now(),
  pi: PI_HOST,
  rosbridge: { up: false, since: null, err: null, attempts: 0 },
  ports: { 22: null, 8080: null, 9090: null },
  system: null,          // ssh 取得
  topics: {},            // topic → {label,hint,count,last,lastTs,hz,raw}
  camera: { ok: null, ms: null, bytes: null, lastTs: null, err: null },
  pwm: { busy: false, text: null, at: null, err: null },
  servo: null,                 // 最後一次致動記錄 {sent, actual, mismatch}
  servoOffsets: null,          // 出廠偏移讀回（注意：ID2 原為 10，本 session 被誤寫為 0）
  motion: { enabled: false, note: "服端已就緒；UI 需手動解鎖" },
  counters: { msgs: 0, snaps: 0 },
};
for (const t of TOPICS) state.topics[t.topic] = { ...t, count: 0, last: null, lastTs: null, hz: 0, _win: 0 };

// ================= rosbridge =================
let ws = null;
let pending = false;
let sched = null;
const isOpen = () => ws && ws.readyState === 1;

function safeSend(obj) {
  if (!isOpen()) return false;
  try { ws.send(JSON.stringify(obj)); return true; } catch { return false; }
}

function scheduleReconnect(delay = 4000) {
  if (sched) return;                      // ✅ 只准排一次（舊版 error+close 各排一次 → 指數分裂）
  sched = setTimeout(() => { sched = null; connectRos(); }, delay);
}

// ✅ 同一時間只留一條連線嘗試；每條 socket 只允許「結案」一次
function connectRos() {
  if (ws || sched || pending) return;
  pending = true;
  state.rosbridge.attempts++;
  let sock;
  try {
    sock = new WebSocket(ROSBRIDGE);
  } catch (e) {
    pending = false;
    state.rosbridge.err = String(e);
    return scheduleReconnect();
  }
  ws = sock;
  let failed = false;
  const fail = (why) => {
    if (failed) return;
    failed = true;
    pending = false;
    if (ws === sock) ws = null;
    state.rosbridge.up = false;
    state.rosbridge.err = why;
    log(`rosbridge 斷線（${why}）— 4 秒後重試`);
    try { sock.close(); } catch { }
    scheduleReconnect();
  };
  // 死主機的 TCP 會靜靜挂著不报错，必須自己逾時
  const openTimer = setTimeout(() => fail("連線逾時 6s"), 6000);

  sock.addEventListener("open", () => {
    clearTimeout(openTimer);
    pending = false;
    state.rosbridge.up = true;
    state.rosbridge.since = Date.now();
    state.rosbridge.err = null;
    let ok = 0;
    for (const t of TOPICS) if (safeSend({ op: "subscribe", topic: t.topic, type: t.type, throttle_rate: t.throttle_rate })) ok++;
    log(`rosbridge 已連線，訂閱 ${ok}/${TOPICS.length} 個 topic`);
  });
  sock.addEventListener("message", (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (d.op !== "publish") return;
    if (d.topic === "/cmd_vel") MOTION.observed++;        // 用於「有無別人也在發 cmd_vel」偵測
    const s = state.topics[d.topic];
    if (!s) return;
    s.count++; s.lastTs = Date.now(); s._win++; s.raw = d.msg;
    s.last = summarize(d.topic, d.msg);
    state.counters.msgs++;
  });
  sock.addEventListener("close", (e) => fail(`close code=${e.code}`));
  sock.addEventListener("error", () => fail("error"));
}

// 把 ROS 訊息壓成一行人可讀的值
function summarize(topic, m) {
  if (m == null) return null;
  if (topic.endsWith("/battery") || topic.endsWith("/get_distance")) return { kind: "num", value: m.data };
  if (topic.endsWith("/camera_info")) {
    const k = m.k || m.K || [];          // ✅ ROS 2 的 CameraInfo 欄位是小寫 k
    return { kind: "kv", value: { fx: r(k[0]), fy: r(k[4]), cx: r(k[2]), cy: r(k[5]), w: m.width, h: m.height, dst: m.distortion_model } };
  }
  if (topic.includes("imu_raw")) {
    const g = m.angular_velocity || {}, a = m.linear_acceleration || {};
    return { kind: "kv", value: { "gx": r(g.x), "gy": r(g.y), "gz": r(g.z), "ax": r(a.x), "ay": r(a.y), "az": r(a.z) } };
  }
  if (topic.includes("/joy")) return { kind: "kv", value: { axes: (m.axes || []).length, buttons: (m.buttons || []).length } };
  if (topic.includes("/sbus")) return { kind: "kv", value: { channels: (m.channels || []).length } };
  if (topic.includes("/button")) return { kind: "kv", value: { button: m.button, event: m.event } };
  return { kind: "raw", value: JSON.stringify(m).slice(0, 120) };
}
const r = (x) => (typeof x === "number" ? Math.round(x * 1000) / 1000 : x);

// ================= SSH =================
function ssh(cmd, timeout = 9000) {
  return new Promise((resolve) => {
    execFile("ssh", ["-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=accept-new", PI_SSH, cmd],
      { timeout, maxBuffer: 4 << 20 }, (err, stdout, stderr) =>
        resolve({ ok: !err, code: err ? (err.code ?? 1) : 0, out: (stdout || "").trim(), err: (stderr || err?.message || "").trim().slice(0, 400) }));
  });
}

const SYS_CMD = [
  "echo THR=$(vcgencmd get_throttled | cut -d= -f2)",
  "echo VOLT=$(vcgencmd measure_volts | cut -d= -f2)",
  "echo TEMP=$(vcgencmd measure_temp | cut -d= -f2)",
  "echo LOAD=$(cut -d' ' -f1-3 /proc/loadavg)",
  "echo UP=$(awk '{print int($1)}' /proc/uptime)",
  "echo BOOTID=$(cat /proc/sys/kernel/random/boot_id)",
  "echo DISK=$(df -h / | awk 'NR==2{print $4\" free / \"$5\" used\"}')",
  "echo SVC=$(systemctl is-active start_node 2>/dev/null)",
  "echo CONT=$(docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | head -3)",
  "echo NODES=$(timeout 8 docker exec -u ubuntu -w /home/ubuntu TurboPi /bin/zsh -c 'source ~/.zshrc >/dev/null 2>&1; ros2 node list 2>/dev/null | wc -l')",
].join("; ");

let sysBusy = false;
async function pollSystem() {
  if (sysBusy) return;
  sysBusy = true;
  try {
    const res = await ssh(SYS_CMD, 16000);
    if (!res.ok) {
      state.system = { up: false, err: res.err || "ssh 失敗", at: Date.now() };
    } else {
      const kv = Object.fromEntries(res.out.split("\n").filter(Boolean).map((l) => {
        const i = l.indexOf("=");
        return [l.slice(0, i), l.slice(i + 1)];
      }));
      state.system = {
        up: true, at: Date.now(),
        throttled: kv.THR || "NA",
        throttledFlags: decodeThrottled(kv.THR),
        volts: kv.VOLT, temp: kv.TEMP, load: kv.LOAD,
        uptimeS: Number(kv.UP || 0), bootId: (kv.BOOTID || "").slice(0, 8),
        disk: kv.DISK, svc: kv.SVC, container: kv.CONT,
        rosNodes: Number(kv.NODES || 0) || null,
      };
    }
  } finally { sysBusy = false; }
}

function decodeThrottled(hex) {
  const bits = parseInt((hex || "").replace(/^0x/i, ""), 16);
  if (!Number.isFinite(bits)) return ["無法解讀"];
  if (bits === 0) return [];
  const names = [[0x1, "目前偵測到供電不足"], [0x2, "ARM 頻率已設上限"], [0x4, "目前正在降頻"],
    [0x8, "溫度上限生效"], [0x10000, "曾發生供電不足"], [0x20000, "曾發生頻率設限"],
    [0x40000, "曾發生降頻"], [0x80000, "曾觸發溫度上限"]];
  const out = [];
  for (const [b, n] of names) if (bits & b) out.push(`0x${b.toString(16)} ${n}`);
  return out;
}

// ================= TCP 探測 =================
function probePort(port) {
  return new Promise((resolve) => {
    const s = net.connect({ host: PI_HOST, port });
    const done = (v) => { s.destroy(); resolve(v); };
    s.setTimeout(1500, () => done(false));
    s.once("connect", () => done(true));
    s.once("error", () => done(false));
  });
}
async function probePorts() {
  const [a, b, c] = await Promise.all([probePort(22), probePort(8080), probePort(9090)]);
  state.ports = { 22: a, 8080: b, 9090: c };
}

// ================= 相機 snapshot proxy（避免瀏覽器直連的 CORS/快取問題）==========
async function fetchSnapshot() {
  const t0 = Date.now();
  try {
    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), 5000);
    const res = await fetch(SNAPSHOT_URL, { signal: ctrl.signal });
    clearTimeout(to);
    if (!res.ok) throw new Error(`http ${res.status}`);
    const buf = Buffer.from(await res.arrayBuffer());
    state.camera = { ok: true, ms: Date.now() - t0, bytes: buf.length, lastTs: Date.now(), err: null };
    state.counters.snaps++;
    return buf;
  } catch (e) {
    state.camera = { ...state.camera, ok: false, err: `${e.name}: ${e.message}`, ms: Date.now() - t0 };
    return null;
  }
}

// ================= 雲台控制（唯一會致動的端點，保護機制全部在**服端**）================
// 實測依據：topic /ros_robot_controller/pwm_servo/set_state（fire-and-forget，無回执）
//   tracking.py: state_x.id=[2] → pan（水平），state_y.id=[1] → tilt（垂直）
const SERVO = {
  LIMITS: { min: 1000, max: 2000 },       // 超出直接拒（硬停機）
  CENTER: 1500,
  MIN_INTERVAL_MS: 500,                   // 節流：不允許連發
  lastAt: 0, busy: false, history: [],
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 🔴 SDK 的讀路徑不可靠，必須繞開它的陷阱：
 *   pwm_servo_read_and_unpack(): `queue.get(block=True)` 拿「隊列裡下一個」，
 *   不核對回包的 servo_id 與命令種類；pos 用 <BBH、off 用 <BBb 解不同長度封包。
 *   → 一次 service 呼叫交錯做 4 次讀取時會拿到**錯位的值**（實測讀出過 1565 這種不存在的數）。
 *   对策：**每次只讀一個 id 的一個欄位**，而且讀兩次取一致，不一致就回報 unknown 而不是编數。 */
const PWM_READ = (id, field) =>
  `sudo -n docker exec -u ubuntu -w /home/ubuntu TurboPi /bin/zsh -c ` +
  `'source ~/.zshrc >/dev/null 2>&1; timeout 25 ros2 service call /ros_robot_controller/pwm_servo/get_state ` +
  `ros_robot_controller_msgs/srv/GetPWMServoState "${field === "offset"
    ? `{cmd: [{id: ${id}, get_position: 0, get_offset: 1}]}`
    : `{cmd: [{id: ${id}, get_position: 1, get_offset: 0}]}`}"'`;

async function readOne(id, field) {
  const res = await ssh(PWM_READ(id, field), 40000);
  const m = (res.out || res.err || "").match(new RegExp(`${field}=\\[\\s*(-?\\d+)`));
  return m ? Number(m[1]) : null;
}

async function readServo(field = "position") {
  const a1 = await readOne(1, field); await sleep(250);
  const a2 = await readOne(2, field); await sleep(250);
  const b1 = await readOne(1, field); await sleep(200);
  const b2 = await readOne(2, field);
  const out = { field, id1: a1 === b1 ? a1 : null, id2: a2 === b2 ? a2 : null,
                raw: { id1: [a1, b1], id2: [a2, b2] }, unreliable: a1 !== b1 || a2 !== b2 };
  if (field === "offset") state.servoOffsets = out;
  return out;
}


function servoGuard(reason, code = 400) {
  return { code, body: { ok: false, error: reason } };
}

async function moveServo({ ids, positions, duration = 0.5 }) {
  // 1) 供電安全門：有降頻/供電不足記錄時不允許致動（舵機突波會讓 brownout 更糟）
  const thr = state.system?.throttled;
  if (thr && thr !== "0x0") return servoGuard(`供電異常（throttled=${thr}），拒絕致動。先排除電源問題再玩雲台。`, 409);
  // 2) 節流與併發鎖
  const now = Date.now();
  if (SERVO.busy) return servoGuard("上一條指令還在執行中", 429);
  if (now - SERVO.lastAt < SERVO.MIN_INTERVAL_MS) return servoGuard(`指令太密，請大於 ${SERVO.MIN_INTERVAL_MS} ms`, 429);
  // 3) 範围硬夾（只接受整數、只接受 ID 1/2）
  const clean = [];
  for (let i = 0; i < ids.length; i++) {
    const id = Number(ids[i]), p = Math.round(Number(positions[i]));
    if (![1, 2].includes(id)) return servoGuard(`id 只能是 1 或 2（收到 ${ids[i]}）`);
    if (!Number.isFinite(p) || p < SERVO.LIMITS.min || p > SERVO.LIMITS.max)
      return servoGuard(`position 必須在 ${SERVO.LIMITS.min}–${SERVO.LIMITS.max}（收到 ${positions[i]}）`);
    clean.push({ id, p });
  }
  const dur = Math.min(3, Math.max(0.05, Number(duration) || 0.5));

  SERVO.busy = true; SERVO.lastAt = now;
  try {
    /* ✅ 兩個關鍵修正：
     * 1) offset 必須傳**空清單**。厂家寫的是 `if i.id and i.offset:` —— 傳 [0] 是「非空清單」
     *    → truthy → 會执行 pwm_servo_set_offset(id, 0)，**把出廠校准歸零**（本 session 已誤觸 ID2: 10→0）。
     * 2) 不再用 `ros2 topic pub --once`：那是「起節點→發一則→銷毀」，volatile QoS 下
     *    DDS 探索未完成時那一則很可能根本没送達（本 session 步驟 4 疑似就丢了）。
     *    改用**已建立的 rosbridge 連線**發布，並連發 3 次（指令冪等，重發無害）。 */
    const msg = { state: clean.map((c) => ({ id: [c.id], position: [c.p], offset: [] })), duration: dur };
    let sent = 0;
    for (let k = 0; k < 3; k++) {
      if (safeSend({ op: "publish", topic: "/ros_robot_controller/pwm_servo/set_state",
                     type: "ros_robot_controller_msgs/msg/SetPWMServoState", msg })) sent++;
      await sleep(130);
    }
    if (!sent) return { code: 503, body: { ok: false, error: "rosbridge 未連線，無法安全發布（不用 ros2 CLI 以免探索競態）" } };
    await sleep(900);                                  // 等舵機走到定位再讀回
    const back = await readServo("position");           // ✅ 開環 → 用雙次一致的讀回變成「有驗證的閉環」
    const sent2 = Object.fromEntries(clean.map((c) => [c.id, c.p]));
    const actual = { 1: back.id1, 2: back.id2 };
    const mismatch = back.unreliable
      ? [`讀回兩次不一致（SDK queue 錯位），無法確認位置：${JSON.stringify(back.raw)}`]
      : clean.filter((c) => actual[c.id] !== c.p && actual[c.id] !== null)
              .map((c) => `ID${c.id} 命令 ${c.p} → 實測 ${actual[c.id]}`);
    const rec = { at: Date.now(), sent: sent2, actual, duration: dur, publishes: sent, mismatch, unreliable: back.unreliable };
    SERVO.history.unshift(rec); SERVO.history = SERVO.history.slice(0, 12);
    state.servo = rec;
    return { code: mismatch.length ? 206 : 200, body: { ok: true, ...rec, err: mismatch.length ? mismatch.join("；") : null } };
  } finally {
    SERVO.busy = false;
  }
}

const PWM_CMD = `（已淘汰：交錯讀 pos/off 會錯位，改走 readServo(field)）`;
async function readPwm() {
  if (state.pwm.busy) return { busy: true };
  state.pwm.busy = true;
  try {
    const p = await readServo("position");
    const o = await readServo("offset");
    state.pwm = {
      busy: false, at: Date.now(), success: !p.unreliable && !o.unreliable,
      positions: [p.id1, p.id2], offsets: [o.id1, o.id2],
      rawPos: p.raw, rawOff: o.raw,
      err: (p.unreliable || o.unreliable) ? "讀回兩次不一致（SDK queue 錯位），此值不可信" : null,
    };
    return state.pwm;
  } catch (e) {
    state.pwm = { busy: false, at: Date.now(), text: null, err: String(e) };
    return state.pwm;
  }
}

// ================= 供電判讀（本輪最重要的一張卡）================
function powerVerdict() {
  const b = state.topics["/ros_robot_controller/battery"];
  const v = b?.last?.value;
  if (typeof v !== "number") return { text: "尚無 battery 資料", level: "unknown" };
  if (v > 6000) return { text: `>6000 ⇒ 這欄位量的是 2S 電池總壓(mV)。滿電 ~8400，建議下限 ~6600`, level: v < 6600 ? "warn" : "good", mv: v };
  if (v >= 4300 && v <= 5700) return { text: `≈${(v / 1000).toFixed(2)} V ⇒ 這欄位量的是 **5V 電源軌**（隨外部電源品質跳動），不是電池電量 ⇒ 電池剩餘仍不可知`, level: v < 4700 ? "bad" : "warn", mv: v };
  return { text: "數值異常，需用三用電表比對", level: "unknown", mv: v };
}

/* ================= 四轮驅動（致動）=================
 * 🔴 必須有看門狗的原因（本 session 源碼查證）：
 *   controller/mecanum.py 只在 cmd_vel_callback 裡算一次、發一次，
 *   **無 create_timer、無 timeout、無自動停止** → 最後一筆輪速會永久保持。
 *   瀏覽器分頁當掉 / WiFi 斷線 / Node 程序被 Ctrl-C ⇒ 車會一直跑到沒電。
 *   ⇒ 看門狗必須在**服端**（瀏覽器正是會死的那一端）。心跳方向也是 client→server，
 *     所以「沒收到心跳就歸零」才能抓到 client 死掉的情况。
 * 發布走 rosbridge（已建立、已探索的連線），不用 ros2 CLI（每秒 7 次 CLI 會壓垮 Pi）。 */
const MOTION = {
  /* 單位換算（本輪源碼查證，mecanum.py）：
   *   speed_convert(speed, max_speed=1.0) = (speed/1.0)*100 → **占空比 %**
   *   線性：duty = |v| × 100      角速度：duty = |wz| × (wheelbase+track)/2 × 100 = |wz| × 13.89
   * ⇒ 上次測試用 vx=0.03 / wz=0.12 實際只有 3% / 1.7% 占空比，**低於静摩擦門檻**
   *   → 實測「聽到馬達聲但沒位移」。以 0.20 m/s ≐ 20% 占空比為測試上限。
   * ⚠ 注意：wz 那項沒除輪半徑（且 max_angular_speed 宣告了從未被用）→
   *   要產生 20% 占空比需要 wz ≈ 1.44 rad/s，比直覺上大很多。這是厂家的標度缺陷。 */
  LIMITS: { vx: 0.25, vy: 0.25, wz: 1.60 },   // 服端硬夾 ≈ 25% / 25% / 22% 占空比
  DUTY: { linear: 100, angular: 13.89 },
  BEAT_MS: 150,             // 看門狗發布週期
  HEARTBEAT_TIMEOUT: 700,   // client 心跳逾時 → 歸零解除
  MAX_ARM_MS: 20000,        // 連續時限
  SONAR_MIN: 200,           // 前方互鎖（只朝前，不等於避障）
  armed: false, estopped: false, checking: false, reason: null,
  vx: 0, vy: 0, wz: 0, lastBeat: 0, armedAt: 0, timer: null,
  publishes: 0, stops: 0, observed: 0, sonarHold: false,
};
const clamp = (v, m) => Math.max(-m, Math.min(m, Number(v) || 0));

function publishTwist(vx, vy, wz) {
  return safeSend({ op: "publish", topic: "/cmd_vel", type: "geometry_msgs/msg/Twist",
    msg: { linear: { x: vx, y: vy, z: 0 }, angular: { x: 0, y: 0, z: wz } } });
}

function motionStop(reason, estop = false) {
  if (MOTION.timer) { clearInterval(MOTION.timer); MOTION.timer = null; }
  let n = 0;
  for (let k = 0; k < 3; k++) if (publishTwist(0, 0, 0)) n++;
  MOTION.armed = false; MOTION.vx = MOTION.vy = MOTION.wz = 0;
  MOTION.estopped = estop || MOTION.estopped;
  MOTION.stops++;
  MOTION.reason = `${reason}（已發 ${n} 筆零速）`;
  log(`🛑 驅動解除：${reason}`);
  return MOTION.reason;
}

function motionTick() {
  const now = Date.now();
  if (!state.rosbridge.up) return motionStop("rosbridge 斷線");
  if (now - MOTION.lastBeat > MOTION.HEARTBEAT_TIMEOUT) return motionStop("client 心跳遺失");
  if (now - MOTION.armedAt > MOTION.MAX_ARM_MS) return motionStop(`達連續 ${MOTION.MAX_ARM_MS / 1000} 秒時限`);
  const thr = state.system?.throttled;
  if (thr && thr !== "0x0") return motionStop(`供電異常 throttled=${thr}`);
  let vx = MOTION.vx, vy = MOTION.vy, wz = MOTION.wz;
  const sonar = state.topics["/sonar_controller/get_distance"]?.last?.value;
  MOTION.sonarHold = !!(sonar && sonar < MOTION.SONAR_MIN && vx > 0);   // 只擋前進
  if (MOTION.sonarHold) vx = 0;
  if (publishTwist(vx, vy, wz)) MOTION.publishes++;
}

async function motionArm() {
  if (MOTION.armed) return { code: 200, body: { ok: true, armed: true } };
  if (MOTION.estopped) return { code: 409, body: { ok: false, error: "已被紧急停用，必須重啟 server 才能再武裝" } };
  if (!state.rosbridge.up) return { code: 503, body: { ok: false, error: "rosbridge 未連線" } };
  const thr = state.system?.throttled;
  if (thr && thr !== "0x0") return { code: 409, body: { ok: false, error: `供電異常（throttled=${thr}），拒絕武裝` } };

  /* 預檢：實測 /cmd_vel 上已有 5 個厂家節點在發布（手勢/追蹤/巡線/QR/避障）。
   * 有別人正在發就拒絕接手，否則兩個來源會互相打方向。 */
  MOTION.checking = true; MOTION.observed = 0;
  safeSend({ op: "subscribe", topic: "/cmd_vel", type: "geometry_msgs/msg/Twist", throttle_rate: 50 });
  await sleep(2200);
  safeSend({ op: "unsubscribe", topic: "/cmd_vel" });
  MOTION.checking = false;
  const others = MOTION.observed;
  await sleep(250);
  MOTION.observed = 0;
  if (others > 0) return { code: 409, body: { ok: false, error: `預檢失敗：2.2 秒內 /cmd_vel 上已有 ${others} 筆別人發的指令，先关掉视觉跟随/巡線等節點` } };

  MOTION.armed = true; MOTION.armedAt = Date.now(); MOTION.lastBeat = Date.now();
  MOTION.vx = MOTION.vy = MOTION.wz = 0;
  publishTwist(0, 0, 0);
  MOTION.timer = setInterval(motionTick, MOTION.BEAT_MS);
  log("🟢 驅動已武裝（看門狗啟動，150 ms）");
  return { code: 200, body: { ok: true, armed: true, limits: MOTION.LIMITS } };
}

function motionSnapshot() {
  const now = Date.now();
  return {
    armed: MOTION.armed, estopped: MOTION.estopped, reason: MOTION.reason,
    limits: MOTION.LIMITS, duty: MOTION.DUTY, beatMs: MOTION.BEAT_MS, maxArmMs: MOTION.MAX_ARM_MS,
    sonarMin: MOTION.SONAR_MIN, sonarHold: MOTION.sonarHold,
    dutyNow: { vx: +(Math.abs(MOTION.vx) * MOTION.DUTY.linear).toFixed(1),
               vy: +(Math.abs(MOTION.vy) * MOTION.DUTY.linear).toFixed(1),
               wz: +(Math.abs(MOTION.wz) * MOTION.DUTY.angular).toFixed(1) },
    sonarMin: MOTION.SONAR_MIN, sonarHold: MOTION.sonarHold,
    vx: MOTION.vx, vy: MOTION.vy, wz: MOTION.wz,
    heartbeatAge: MOTION.armed ? now - MOTION.lastBeat : null,
    remainMs: MOTION.armed ? Math.max(0, MOTION.MAX_ARM_MS - (now - MOTION.armedAt)) : null,
    publishes: MOTION.publishes, stops: MOTION.stops,
  };
}

// ================= HTTP + SSE =================
const sse = new Set();
function broadcast() {
  const now = Date.now();
  // 心跳才是存活依據（瀏覽器與腳本都靠 beat 續命；分頁死掉 ⇒ 心跳停 ⇒ 700 ms 解除）。
  // 不再用「有無 SSE 客戶端」當依據：純 API 測試時本來就沒有瀏覽器連線。
  if (MOTION.armed && now - MOTION.lastBeat > MOTION.HEARTBEAT_TIMEOUT) motionStop("client 心跳遺失");
  for (const t of Object.values(state.topics)) {
    t.hz = Math.round((t._win / 2) * 10) / 10;   // 每 2 秒視窗
    t._win = 0;
    t.stale = !t.lastTs || now - t.lastTs > 5000;
    t.ageS = t.lastTs ? Math.round((now - t.lastTs) / 1000) : null;
  }
  const payload = JSON.stringify({ ...state, ts: now, power: powerVerdict(), motion: motionSnapshot() });
  for (const res of sse) { try { res.write(`data: ${payload}\n\n`); } catch { } }
}

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, `http://localhost:${PORT}`);
  const send = (code, body, type = "application/json; charset=utf-8") => {
    res.writeHead(code, { "Content-Type": type, "Cache-Control": "no-store" });
    res.end(body);
  };

  if (u.pathname === "/" || u.pathname === "/index.html") {
    return send(200, fs.readFileSync(path.join(HERE, "index.html")), "text/html; charset=utf-8");
  }
  if (u.pathname === "/events") {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
    res.write("retry: 1500\n\n");
    sse.add(res);
    req.on("close", () => sse.delete(res));
    return;
  }
  if (u.pathname === "/api/state") return send(200, JSON.stringify({ ...state, power: powerVerdict(), motion: motionSnapshot() }, null, 2));
  if (u.pathname === "/snapshot.jpg") {
    const buf = await fetchSnapshot();
    if (!buf) return send(502, Buffer.from(""));
    res.writeHead(200, { "Content-Type": "image/jpeg", "Cache-Control": "no-store" });
    return res.end(buf);
  }
  if (u.pathname === "/api/pwm_state" && req.method === "POST") {
    const out = await readPwm();
    return send(200, JSON.stringify(out));
  }
  // 致動端點：只能從本機呼叫（server 只監 127.0.0.1），且有服端保護
  if (u.pathname === "/api/servo" && req.method === "POST") {
    let body = ""; req.on("data", (c) => { body += c; if (body.length > 4000) req.destroy(); });
    req.on("end", async () => {
      let j = {};
      try { j = JSON.parse(body || "{}"); } catch { }
      let out;
      if (j.center) {
        out = await moveServo({ ids: [1, 2], positions: [SERVO.CENTER, SERVO.CENTER], duration: 0.8 });
      } else {
        out = await moveServo({ ids: [j.id], positions: [j.position], duration: j.duration });
      }
      log(`致動 id=${j.id ?? "1+2"} pos=${j.position ?? SERVO.CENTER} → ${out.code} ${out.body.error || ""}`);
      send(out.code, JSON.stringify(out.body));
    });
    return;
  }
  // 四轮驅動端點（預設鎖定；看門狗與夾限全部在服端）
  if (u.pathname.startsWith("/api/motion/") && req.method === "POST") {
    let body = ""; req.on("data", (c) => { body += c; if (body.length > 2000) req.destroy(); });
    req.on("end", async () => {
      let j = {};
      try { j = JSON.parse(body || "{}"); } catch { }
      if (u.pathname === "/api/motion/arm") {
        const out = await motionArm();
        return send(out.code, JSON.stringify(out.body));
      }
      if (u.pathname === "/api/motion/clear_estop") {
        // 解除急停需要**人主動按一下**（不是自動解），保留「人為確認」這個意義但不用重啟 server
        MOTION.estopped = false;
        MOTION.reason = "急停已由使用者明確解除";
        log("⚪ 急停已由使用者明確解除");
        return send(200, JSON.stringify({ ok: true, estopped: false }));
      }
      if (u.pathname === "/api/motion/stop") {
        const why = j.estop ? "使用者按下 STOP" : (j.reason || "client 要求解除");
        return send(200, JSON.stringify({ ok: true, armed: false, estopped: !!j.estop || MOTION.estopped,
                                          note: motionStop(why, !!j.estop) }));
      }
      // beat：带心跳與速度向量（未武裝一律拒）
      if (!MOTION.armed) return send(409, JSON.stringify({ ok: false, error: "未武裝，拒绝驅動指令" }));
      MOTION.vx = clamp(j.vx, MOTION.LIMITS.vx);
      MOTION.vy = clamp(j.vy, MOTION.LIMITS.vy);
      MOTION.wz = clamp(j.wz, MOTION.LIMITS.wz);
      MOTION.lastBeat = Date.now();
      return send(200, JSON.stringify({ ok: true, ...motionSnapshot() }));
    });
    return;
  }
  send(404, JSON.stringify({ error: "not found" }));
});

// ================= 排程 =================
const log = (m) => console.log(`[${new Date().toLocaleTimeString()}] ${m}`);
// 護欄：任何上游（ssh / rosbridge / http）的例外都不該讓儀表板整個退出
process.on("uncaughtException", (e) => log(`未捕捉例外: ${e?.message || e}`));
process.on("unhandledRejection", (e) => log(`未處理 Promise: ${e?.message || e}`));
setInterval(broadcast, 2000);
setInterval(probePorts, 5000);
setInterval(pollSystem, 6000);
probePorts(); pollSystem(); connectRos();

process.on("SIGINT", () => { try { if (MOTION.armed) for (let k = 0; k < 3; k++) publishTwist(0, 0, 0); } catch { } process.exit(0); });
process.on("SIGTERM", () => { try { if (MOTION.armed) for (let k = 0; k < 3; k++) publishTwist(0, 0, 0); } catch { } process.exit(0); });

server.listen(PORT, "127.0.0.1", () => {
  console.log(`TurboPi 儀表板 http://localhost:${PORT}   （目標 Pi: ${PI_HOST}）`);
  console.log("致動端點已就緒但**預設鎖定**：需 UI 明確解鎖/武裝；看門狗 150 ms、心跳逾時 700 ms 自動歸零。");
});
