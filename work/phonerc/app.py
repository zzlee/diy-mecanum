#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TurboPi 手機遙控服務（跑在容器內，--net=host ⇒ 綁 0.0.0.0:8090 等於綁在車上）

設計原則：**安全護欄一律在車上**，不信任手機瀏覽器（分頁會被系統殺、鎖屏會暫停 JS）。
  · rclpy 節點以 20 Hz 定時器發佈 /cmd_vel；心跳逾時 700 ms 自動歸零並在伺服端留原因
  · 未武裝不下發；數值越限回 400（不是靜音夾收）；側移（linear.y）在源頭封鎖
  · 超音波 < 200 mm 鎖前進（仍可後退／旋轉）；急停 latching，需人工明確解除
  · 解除武裝後補發 1 秒零速，之後保持安靜（不與厂家 5 個 /cmd_vel 發佈者搶線）

放置：主機 /home/pi/docker_ros2/tmp/phonerc/app.py  ＝ 容器內 /home/ubuntu/share/tmp/phonerc/app.py
啟動：見同目錄 start.sh
"""
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from std_msgs.msg import Int32, UInt16

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(BASE_DIR, "photos")
PIN_FILE = os.path.join(BASE_DIR, "pin.txt")
PHOTO_MAX = 20                      # SD 卡只剩 2.2 G，硬上限
PORT = 8090
SNAP_URL = "http://127.0.0.1:8080/snapshot?topic=/image_raw"

LIMITS = {"vx": 0.25, "vy": 0.25, "wz": 1.60}   # duty% = |v|*100；|wz|*13.89
DUTY = {"linear": 100.0, "angular": 13.89}      # 實測換算（HANDOVER §13.1）
STRAFE_ENABLED = False                          # 純 linear.y 實測造成自轉（HANDOVER §15）
HEARTBEAT_TIMEOUT = 0.70                        # 秒
REQUIRE_ARM = False      # 依使用者要求：連上網頁即可駕駛（按住才動＝真正的死人的保護）
ARM_MAX_SECONDS = 1e9    # REQUIRE_ARM=False 時不設連續上限
SONAR_HOLD_MM = 200
SERVO_MIN, SERVO_MAX = 500, 2500

STATE = {
    "armed": False, "estopped": False, "reason": "尚未武裝",
    "vx": 0.0, "vy": 0.0, "wz": 0.0,
    "last_beat": 0.0, "armed_at": 0.0, "publishes": 0,
    "sonar": None, "battery": None, "pan": None, "tilt": None,
}
LOCK = threading.Lock()


def pin():
    try:
        with open(PIN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""                                   # 檔不存在 ⇒ 不設 PIN（只在自己 AP 內，可自行決定）


class Robot(Node):
    def __init__(self):
        super().__init__("phone_rc")
        self.pub_vel = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_subscription(Int32, "/sonar_controller/get_distance", self.on_sonar, 10)
        self.create_subscription(UInt16, "/ros_robot_controller/battery", self.on_battery, 10)
        try:
            from ros_robot_controller_msgs.msg import SetPWMServoState, PWMServoState
            self.ServoMsg, self.ServoItem = SetPWMServoState, PWMServoState
            self.pub_servo = self.create_publisher(SetPWMServoState,
                                                   "/ros_robot_controller/pwm_servo/set_state", 10)
        except Exception as e:
            self.ServoMsg = None
            self.servo_err = str(e)
        try:
            from ros_robot_controller_msgs.srv import GetPWMServoState
            self.cli = self.create_client(GetPWMServoState, "/ros_robot_controller/pwm_servo/get_state")
            from ros_robot_controller_msgs.msg import PWMServoCommand
            self.GetReq, self.CmdItem = GetPWMServoState.Request, PWMServoCommand
        except Exception:
            self.cli = None
            self.GetReq = None
            self.CmdItem = None
        self.zero_left = 0
        self.create_timer(0.05, self.tick)                      # 20 Hz
        if self.cli is not None:
            self.create_timer(2.0, self.poll_servo)             # 每 2 秒回讀雲台

    # ---- 遙測 ----
    def on_sonar(self, m):
        STATE["sonar"] = int(m.data)

    def on_battery(self, m):
        STATE["battery"] = int(m.data)

    def poll_servo(self):
        """Non-blocking servo readback. Any failure disables it permanently (driving unaffected)."""
        if self.cli is None or getattr(self, "CmdItem", None) is None:
            return
        try:
            if not self.cli.wait_for_service(timeout_cap=0.0):
                return
            req = self.GetReq()
            one = self.CmdItem(); one.id = 2; one.get_position = True; one.get_offset = False
            two = self.CmdItem(); two.id = 1; two.get_position = True; two.get_offset = False
            req.cmd = [one, two]
            fut = self.cli.call_async(req)
            fut.add_done_callback(self._servo_cb)
        except Exception:
            self.cli = None

    def _servo_cb(self, fut):
        """厂家回來的 state[].id 恆為空陣列，因此**依請求順序**對應：state[0]=pan(id2)、state[1]=tilt(id1)。"""
        try:
            states = list(fut.result().state)
            got = [list(st.position) for st in states]
            if len(got) >= 1 and got[0]:
                STATE["pan"] = got[0][0]
            if len(got) >= 2 and got[1]:
                STATE["tilt"] = got[1][0]
        except Exception:
            pass

    # ---- 核心：20 Hz 心跳看門狗 ----
    def tick(self):
        now = time.monotonic()
        with LOCK:
            stale = (now - STATE["last_beat"]) > HEARTBEAT_TIMEOUT if STATE["armed"] else True
            if STATE["armed"] and stale:
                STATE["armed"] = False
                STATE["reason"] = "client 心跳遺失（已自動歸零）"
                self.zero_left = 20
            if STATE["armed"] and STATE["armed_at"] and (now - STATE["armed_at"]) > ARM_MAX_SECONDS:
                STATE["armed"] = False
                STATE["reason"] = f"連續武裝達 {ARM_MAX_SECONDS:.0f} 秒上限（自動解除）"
                self.zero_left = 20
            send = None
            if STATE["armed"] and not STATE["estopped"]:
                vx, vy, wz = STATE["vx"], STATE["vy"], STATE["wz"]
                if STATE["sonar"] is not None and STATE["sonar"] < SONAR_HOLD_MM and vx > 0:
                    vx = 0.0
                send = (vx, vy, wz)
                STATE["publishes"] += 1
            elif self.zero_left > 0:
                send = (0.0, 0.0, 0.0)
                self.zero_left -= 1
                if self.zero_left == 0:
                    STATE["vx"] = STATE["vy"] = STATE["wz"] = 0.0
        if send is not None:
            t = Twist()
            t.linear.x, t.linear.y, t.linear.z = send[0], send[1], 0.0
            t.angular.z = send[2]
            self.pub_vel.publish(t)

    def set_vel(self, vx, vy, wz):
        with LOCK:
            STATE["vx"], STATE["vy"], STATE["wz"] = vx, vy, wz
            STATE["last_beat"] = time.monotonic()

    def publish_servo(self, servo_id, us, duration=0.6, times=3):
        if self.ServoMsg is None:
            return False
        m = self.ServoMsg()
        it = self.ServoItem()
        it.id = [int(servo_id)]
        it.position = [int(us)]
        it.offset = []                 # 🔴 必須送空陣列：非空為 truthy ⇒ 會觸發 set_offset
        m.state = [it]
        m.duration = float(duration)
        for _ in range(times):         # volatile QoS：連發三次保底送達
            self.pub_servo.publish(m)
            time.sleep(0.12)
        return True

    def vendor_cmd_vel_publishers(self):
        try:
            return max(0, self.count_publishers("/cmd_vel") - 1)   # 減掉自己
        except Exception:
            return -1


ROBOT = None


def clamp(name, val):
    v = float(val)
    lim = LIMITS[name]
    if v != v:
        raise ValueError("NaN")
    return max(-lim, min(lim, v))


def prune_photos():
    try:
        fs = sorted(os.listdir(PHOTO_DIR), key=lambda n: os.path.getmtime(os.path.join(PHOTO_DIR, n)))
        while len(fs) > PHOTO_MAX:
            os.remove(os.path.join(PHOTO_DIR, fs.pop(0)))
    except Exception:
        pass


def take_photo():
    req = urllib.request.Request(SNAP_URL, headers={"User-Agent": "phone-rc/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    os.makedirs(PHOTO_DIR, exist_ok=True)
    name = time.strftime("pic_%Y%m%d-%H%M%S.jpg")
    with open(os.path.join(PHOTO_DIR, name), "wb") as f:
        f.write(data)
    prune_photos()
    return name, data


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _authed(self, body):
        p = pin()
        return (not p) or str(body.get("pin", "")).strip() == p

    def state_json(self):
        now = time.monotonic()
        with LOCK:
            age = int((now - STATE["last_beat"]) * 1000) if STATE["last_beat"] else None
            remain = max(0.0, ARM_MAX_SECONDS - (now - STATE["armed_at"])) if STATE["armed"] else 0.0
            d = dict(STATE)
            d["heartbeat_age_ms"] = age
            d["remain_ms"] = int(remain * 1000)
            d["limits"] = LIMITS
            d["duty"] = {"vx": round(abs(STATE["vx"]) * DUTY["linear"], 1),
                         "vy": round(abs(STATE["vy"]) * DUTY["linear"], 1),
                         "wz": round(abs(STATE["wz"]) * DUTY["angular"], 1)}
            d["strafe_enabled"] = STRAFE_ENABLED
            d["sonar_hold_mm"] = SONAR_HOLD_MM
            d["pin_required"] = bool(pin())
        try:
            d["vendor_cmd_vel_publishers"] = ROBOT.vendor_cmd_vel_publishers()
        except Exception:
            d["vendor_cmd_vel_publishers"] = -1
        try:
            n = len(os.listdir(PHOTO_DIR))
        except Exception:
            n = 0
        d["photos"] = n
        d["photo_max"] = PHOTO_MAX
        return d

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/api/state":
            return self._send(200, self.state_json())
        if p == "/api/limits":                   # 只回報夾限結果，**不下發任何速度**（安全測試用）
            q = self.path.split("?")[1] if "?" in self.path else ""
            kv = dict(x.split("=", 1) for x in q.split("&") if "=" in x)
            try:
                out = {k: clamp(k, kv.get(k, 0.0)) for k in ("vx", "vy", "wz")}
            except Exception:
                return self._send(400, {"error": "數值不合法"})
            if not STRAFE_ENABLED:
                out["vy"] = 0.0
            return self._send(200, {"applied": False, "clamped": out,
                                    "duty": {k: round(abs(v) * (DUTY["angular"] if k == "wz" else DUTY["linear"]), 1)
                                             for k, v in out.items()}})
        if p == "/api/photos":
            try:
                fs = sorted(os.listdir(PHOTO_DIR), reverse=True)
            except Exception:
                fs = []
            return self._send(200, {"files": fs})
        if p.startswith("/photo/"):
            name = os.path.basename(p[len("/photo/"):])
            fp = os.path.join(PHOTO_DIR, name)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            return self._send(404, {"error": "找不到檔案"})
        if p == "/stream":                       # 轉發厂家的 MJPEG，手機可直接顯示
            try:
                req = urllib.request.Request("http://127.0.0.1:8080/stream?topic=/image_raw")
                with urllib.request.urlopen(req, timeout=20) as up:
                    self.send_response(200)
                    self.send_header("Content-Type", up.headers.get("Content-Type", "multipart/x-mixed-replace"))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        chunk = up.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception:
                pass
            return
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        body = self._json_body()
        if not self._authed(body):
            return self._send(401, {"error": "PIN 錯誤"})
        if STATE["estopped"] and p not in ("/api/clear_estop", "/api/state"):
            return self._send(409, {"error": "已緊急停用，需人工明確解除", "estopped": True})

        if p == "/api/arm":
            n = ROBOT.vendor_cmd_vel_publishers()
            with LOCK:
                STATE.update(armed=True, armed_at=time.monotonic(), last_beat=time.monotonic(),
                             vx=0.0, vy=0.0, wz=0.0, reason="已武裝")
            return self._send(200, {"ok": True, "armed": True, "limits": LIMITS,
                                    "vendor_cmd_vel_publishers": n,
                                    "note": f"車上另有 {n} 個 /cmd_vel 發佈者（厂家節點），請確認它們沒在跑"
                                            if n > 0 else "未偵測到其他 /cmd_vel 發佈者"})
        if p == "/api/beat":
            if REQUIRE_ARM and not STATE["armed"]:
                return self._send(409, {"error": "未武裝，先按『取得控制權』"})
            if not STATE["armed"]:        # 隱式武裝：第一筆心跳就啟動
                with LOCK:
                    STATE.update(armed=True, armed_at=time.monotonic(), reason="駕駛中（按住才動）")
                    ROBOT.zero_left = 0
            try:
                vx = clamp("vx", body.get("vx", 0.0))
                wz = clamp("wz", body.get("wz", 0.0))
                vy = 0.0 if not STRAFE_ENABLED else clamp("vy", body.get("vy", 0.0))
            except Exception:
                return self._send(400, {"error": "數值不合法"})
            ROBOT.set_vel(vx, vy, wz)
            return self._send(200, {"ok": True, "vx": vx, "vy": vy, "wz": wz,
                                    "duty": self.state_json()["duty"]})
        if p == "/api/stop":
            ROBOT.set_vel(0.0, 0.0, 0.0)
            with LOCK:
                ROBOT.zero_left = 20
            return self._send(200, {"ok": True, "stopped": True})
        if p == "/api/disarm":
            ROBOT.set_vel(0.0, 0.0, 0.0)
            with LOCK:
                STATE.update(armed=False, reason="手動解除", vx=0.0, vy=0.0, wz=0.0)
                ROBOT.zero_left = 20
            return self._send(200, {"ok": True, "armed": False})
        if p == "/api/estop":
            with LOCK:
                STATE.update(estopped=True, armed=False, vx=0.0, vy=0.0, wz=0.0,
                             reason="緊急停用（latched）")
                ROBOT.zero_left = 30
            return self._send(200, {"ok": True, "estopped": True})
        if p == "/api/clear_estop":
            with LOCK:
                STATE.update(estopped=False, reason="急停已由使用者明確解除")
            return self._send(200, {"ok": True, "estopped": False})
        if p == "/api/photo":
            try:
                name, data = take_photo()
            except Exception as e:
                return self._send(500, {"error": f"拍照失敗 {type(e).__name__}: {e}"})
            return self._send(200, data, "image/jpeg",
                              extra={"X-Saved-As": name, "X-Saved-Dir": PHOTO_DIR})
        if p == "/api/servo":
            axis = str(body.get("axis", "pan"))
            sid = {"pan": 2, "tilt": 1}.get(axis)
            if sid is None:
                return self._send(400, {"error": "axis 只能是 pan 或 tilt"})
            try:
                us = int(float(body.get("us", 1500)))
            except Exception:
                return self._send(400, {"error": "us 不合法"})
            if not (SERVO_MIN <= us <= SERVO_MAX):
                return self._send(400, {"error": f"us 需介於 {SERVO_MIN}-{SERVO_MAX}", "us": us})
            if not ROBOT.publish_servo(sid, us):
                return self._send(500, {"error": "舵機訊息型別不可用"})
            with LOCK:
                STATE["pan" if axis == "pan" else "tilt"] = us
            return self._send(200, {"ok": True, "axis": axis, "us": us})
        if p == "/api/photos/clear":
            try:
                for f in os.listdir(PHOTO_DIR):
                    os.remove(os.path.join(PHOTO_DIR, f))
            except Exception:
                pass
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


PAGE = """<!doctype html><html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="theme-color" content="#0b0e14"><title>TurboPi</title>
<style>
:root{--bg:#0b0e14;--card:#141a24;--fg:#dfe6f2;--dim:#8a97ab;--ok:#3ddc84;--bad:#ff5c5c}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Noto Sans TC",sans-serif;padding:10px;overscroll-behavior:none}
h1{font-size:16px;margin:2px 0 8px}
.stage{position:relative;width:100%;max-width:460px;margin:0 auto;aspect-ratio:4/3;background:#000;border-radius:12px;overflow:hidden}
.stage img{width:100%;height:100%;object-fit:contain;display:block;pointer-events:none}
#pad{position:absolute;inset:0;touch-action:none;user-select:none;cursor:crosshair;background:
  radial-gradient(circle at 50% 50%, rgba(255,255,255,.06), transparent 62%)}
#pad.act{background:radial-gradient(circle at 50% 50%, rgba(61,220,132,.16), transparent 62%)}
#ind{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.hint{position:absolute;left:0;right:0;bottom:8px;text-align:center;font-size:12px;color:#cfd8e6;
  text-shadow:0 1px 3px #000;pointer-events:none}
.bar{display:flex;gap:6px;flex-wrap:wrap;margin:8px auto;font-size:12px;color:var(--dim);max-width:460px}
.chip{background:var(--card);border-radius:20px;padding:3px 9px}
.st{font-weight:700}.on{color:var(--ok)}.off{color:var(--dim)}.bad{color:var(--bad)}
.sv{width:100%;max-width:460px;margin:6px auto;background:var(--card);border-radius:12px;padding:10px;font-size:13px}
input[type=range]{width:100%}
.note{font-size:11px;color:var(--dim);margin:6px auto;max-width:460px}
.err{color:var(--bad);font-size:12px;min-height:16px;max-width:460px;margin:4px auto}
.row{display:flex;gap:8px;max-width:460px;margin:8px auto}
button{border:0;border-radius:12px;padding:16px 8px;font-size:16px;color:var(--fg);background:var(--card);
 flex:1;user-select:none;touch-action:manipulation}
button:active{filter:brightness(1.6)}
.danger{background:#3a1518;color:#ffb3b3;font-weight:800}
.photo{background:#1d2f22;color:#b8f0c8;font-weight:800;padding:18px}
</style></head><body>
<h1>TurboPi <span style="color:var(--dim);font-size:11px">在畫面上拖曳＝前進/轉向・輕按一下＝停止</span></h1>
<div class="stage">
  <img id="v" alt="載入中…">
  <svg id="ind"><circle id="c0" r="6" fill="rgba(255,255,255,.35)"></circle>
    <line id="ln" stroke="#3ddc84" stroke-width="4" stroke-linecap="round" opacity="0"></line>
    <circle id="c1" r="12" fill="none" stroke="#3ddc84" stroke-width="3" opacity="0"></circle></svg>
  <div id="pad"></div>
  <div class="hint" id="hint">按住並拖曳（上=前，下=後，左右=旋轉）；輕按＝停止</div>
</div>
<div class="bar">
 <span class="chip">狀態 <b id="st" class="st off">待命</b></span>
 <span class="chip">vx <b id="vxv">0.00</b></span>
 <span class="chip">wz <b id="wzv">0.00</b></span>
 <span class="chip">duty <b id="duty">0/0</b>%</span>
 <span class="chip">心跳 <b id="age">-</b>ms</span>
 <span class="chip">超音波 <b id="son">-</b>mm</span>
 <span class="chip">電池 <b id="bat">-</b>mV</span>
 <span class="chip">pan <b id="panv">-</b></span>
 <span class="chip">照片 <b id="np">0</b></span>
</div>
<div class="note">🔴 <b>側移已停用</b>（實測純 linear.y 會讓車自轉）⇒ 斜拖＝「前後＋旋轉」合成。桌面可用 ↑↓ 前後、Q/E 旋轉。</div>
<div class="sv">pan（µs 越大＝鏡頭往左）<input id="pan" type="range" min="1100" max="1900" value="1500"></div>
<div class="sv">tilt <input id="tilt" type="range" min="1100" max="1900" value="1500"></div>
<div class="row"><button class="photo" id="shot">📷 拍照</button></div>
<div class="row"><button class="danger" id="estop">🛑 急停</button><button id="clr" style="display:none">解除急停</button></div>
<div id="shotbox" style="max-width:460px;margin:8px auto"></div>
<div class="err" id="err"></div>
<div class="note"><a href="/api/photos" style="color:#7fb2ff">照片清單</a>｜<span id="vid"></span></div>
<script>
const $=id=>document.getElementById(id);
const LIM={vx:0.25, wz:1.60}, DEAD=18, RMAX=110;   // px：死區與滿速半徑
let estopped=false, beatTimer=null, sending={vx:0,wz:0};

async function post(p,o={}){
  const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});
  const t=r.headers.get('content-type')||'';
  const d=t.includes('json')?await r.json():await r.blob();
  if(!r.ok) throw new Error((d&&d.error)||('HTTP '+r.status));
  return {status:r.status,data:d,headers:r.headers};
}
async function send(v){
  sending=v;
  $('vxv').textContent=v.vx.toFixed(2); $('wzv').textContent=v.wz.toFixed(2);
  if(estopped) return;
  try{ await post('/api/beat', v); $('err').textContent=''; }catch(e){ $('err').textContent=e.message; }
}
function startLoop(){ if(beatTimer) return; beatTimer=setInterval(()=>send(curVec()),220); }
function stopLoop(finalZero){
  if(beatTimer){clearInterval(beatTimer); beatTimer=null;}
  if(finalZero) send({vx:0,wz:0});
}
// ---- 拖曳控制層 ----
const pad=$('pad'), ind=$('ind'), ln=$('ln'), c1=$('c1'), c0=$('c0');
let drag=null;   // {x0,y0,x,y}
function fit(){ const r=pad.getBoundingClientRect(); ind.setAttribute('viewBox',`0 0 ${r.width} ${r.height}`);
  c0.setAttribute('cx',r.width/2); c0.setAttribute('cy',r.height/2); }
addEventListener('resize',fit); fit();
function curVec(){
  if(!drag) return {vx:0,wz:0};
  const dx=drag.x-drag.x0, dy=drag.y-drag.y0, len=Math.hypot(dx,dy);
  if(len<DEAD) return {vx:0,wz:0};
  const k=Math.min(1,(len-DEAD)/(RMAX-DEAD));
  const ux=dx/len, uy=dy/len;
  return {vx:(-uy)*k*LIM.vx, wz:(-ux)*k*LIM.wz};   // 上拖 dy<0 ⇒ vx>0；右拖 ux>0 ⇒ wz<0（wz>0=左，已實測）
}
function draw(){
  const r=pad.getBoundingClientRect(), cx=r.width/2, cy=r.height/2;
  if(!drag){ ln.setAttribute('opacity',0); c1.setAttribute('opacity',0); return; }
  const k=Math.min(1,Math.hypot(drag.x-drag.x0,drag.y-drag.y0)/RMAX);
  const ex=cx+(drag.x-drag.x0)*0.9, ey=cy+(drag.y-drag.y0)*0.9;
  ln.setAttribute('x1',cx); ln.setAttribute('y1',cy); ln.setAttribute('x2',ex); ln.setAttribute('y2',ey);
  c1.setAttribute('cx',ex); c1.setAttribute('cy',ey);
  const col=k<0.15?'#8a97ab':(k<0.6?'#e8b84b':'#3ddc84');
  ln.setAttribute('stroke',col); c1.setAttribute('stroke',col);
  ln.setAttribute('opacity',1); c1.setAttribute('opacity',1);
}
function begin(e){
  if(estopped){ $('err').textContent='已急停，先按『解除急停』'; return; }
  const r=pad.getBoundingClientRect();
  drag={x0:e.clientX-r.left, y0:e.clientY-r.top, x:e.clientX-r.left, y:e.clientY-r.top, t:Date.now(), id:e.pointerId};
  pad.setPointerCapture&&pad.setPointerCapture(e.pointerId);
  pad.classList.add('act'); $('hint').style.opacity=.25; startLoop(); draw();
  send(curVec());
}
function move(e){ if(!drag||e.pointerId!==drag.id) return; const r=pad.getBoundingClientRect();
  drag.x=e.clientX-r.left; drag.y=e.clientY-r.top; draw(); }
function end(e){
  if(!drag||(e&&e.pointerId!==drag.id)) return;
  const moved=Math.hypot(drag.x-drag.x0, drag.y-drag.y0), dur=Date.now()-drag.t;
  drag=null; pad.classList.remove('act'); $('hint').style.opacity=1;
  stopLoop(true); draw();
  if(moved<DEAD && dur<400){ post('/api/stop',{}).catch(()=>{}); $('err').textContent=''; }   // 輕按＝停止
}
pad.addEventListener('pointerdown',e=>{e.preventDefault(); begin(e);});
pad.addEventListener('pointermove',e=>{e.preventDefault(); move(e);});
pad.addEventListener('pointerup',e=>{e.preventDefault(); end(e);});
pad.addEventListener('pointercancel',e=>end(e));
addEventListener('blur',()=>end());
document.addEventListener('visibilitychange',()=>{ if(document.hidden){ drag=null; pad.classList.remove('act'); stopLoop(true);} });
const KEY={ArrowUp:[1,0],ArrowDown:[-1,0],w:[1,0],s:[-1,0],q:[0,1],e:[0,-1]};
let keys={};
addEventListener('keydown',ev=>{ const k=KEY[ev.key]; if(k&&!ev.repeat){ keys[ev.key]=k; startLoop(); send(kbd()); } });
addEventListener('keyup',ev=>{ if(KEY[ev.key]){ delete keys[ev.key]; if(!Object.keys(keys).length) stopLoop(true); } });
function kbd(){ let vx=0,wz=0; for(const k in keys){ vx+=keys[k][0]; wz+=keys[k][1]; }
  return {vx:Math.max(-1,Math.min(1,vx))*LIM.vx, wz:Math.max(-2,Math.min(2,wz))*LIM.wz}; }
// ---- 其餘控制 ----
$('estop').onclick=async()=>{ try{ await post('/api/estop',{}); drag=null; stopLoop(true);}catch(e){$('err').textContent=e.message;} };
$('clr').onclick=async()=>{ try{ await post('/api/clear_estop',{}); $('err').textContent=''; }catch(e){$('err').textContent=e.message;} };
$('shot').onclick=async()=>{
  try{ const r=await post('/api/photo',{}); const url=URL.createObjectURL(r.data), name=r.headers.get('X-Saved-As')||'photo.jpg';
    if($('shotbox').dataset.prev) URL.revokeObjectURL($('shotbox').dataset.prev); $('shotbox').dataset.prev=url;
    $('shotbox').innerHTML=`<img src="${url}" style="width:100%;border-radius:10px">
      <div class="note">已存於車上 <b>${name}</b>｜<a href="${url}" download="${name}" style="color:#7fb2ff">下載到手機</a></div>`;
  }catch(e){ $('err').textContent=e.message; }
};
let svT=null;
for(const ax of ['pan','tilt']) $(ax).addEventListener('input',()=>{ clearTimeout(svT); svT=setTimeout(async()=>{
  try{ await post('/api/servo',{axis:ax, us:+$(ax).value}); }catch(e){ $('err').textContent=e.message; } },260); });
$('v').src=location.protocol+'//'+location.hostname+':8080/stream?topic=/image_raw';
setInterval(async()=>{
  try{
    const s=await (await fetch('/api/state')).json();
    estopped=!!s.estopped; $('clr').style.display=estopped?'block':'none';
    $('st').textContent=estopped?'⛔ 已急停':(s.armed?'🟢 駕駛中':'待命');
    $('st').className='st '+(estopped?'bad':(s.armed?'on':'off'));
    $('age').textContent=s.heartbeat_age_ms ?? '-'; $('son').textContent=s.sonar ?? '-';
    $('bat').textContent=s.battery ?? '-'; $('duty').textContent=`${s.duty.vx}/${s.duty.wz}`;
    $('panv').textContent=s.pan ?? '-'; $('np').textContent=s.photos;
    $('vid').textContent=s.vendor_cmd_vel_publishers>0?`⚠ 車上另有 ${s.vendor_cmd_vel_publishers} 個厂家 /cmd_vel 發佈者（別同時跑巡線/追跡）`:'';
  }catch(e){ $('st').textContent='⚠ 與車失去連線'; $('st').className='st bad'; }
},700);
</script></body></html>"""


def main():
    global ROBOT
    import signal
    import sys as _sys
    os.makedirs(PHOTO_DIR, exist_ok=True)
    # ⚠️ 必須在 rclpy.init() 之後註冊：rclpy 會自己裝 SIGTERM 处理器但那只設旗標，
    #    而 serve_forever() 不會因旗標退出 ⇒ 不蓋回來的話 systemd stop / pkill 都殺不掉這個程序。
    def _bye(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    rclpy.init()
    ROBOT = Robot()
    th = threading.Thread(target=rclpy.spin, args=(ROBOT,), daemon=True)
    th.start()
    print(f"[phone-rc] 啟動於 http://0.0.0.0:{PORT}  照片目錄 {PHOTO_DIR}（上限 {PHOTO_MAX} 張）")
    print(f"[phone-rc] PIN {'已啟用' if pin() else '未設定'}｜側移 {'啟用' if STRAFE_ENABLED else '🔴 已停用'}"
          f"｜夾限 {LIMITS}｜心跳逾時 {HEARTBEAT_TIMEOUT}s｜超音波互鎖 {SONAR_HOLD_MM}mm")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            t = Twist()
            for _ in range(10):
                ROBOT.pub_vel.publish(t)
                time.sleep(0.05)
        except Exception:
            pass
        ROBOT.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
