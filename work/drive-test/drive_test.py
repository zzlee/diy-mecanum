#!/usr/bin/env python3
"""
四輪占空比階梯測試（走 webui 服端 API ⇒ 看門狗與夾限一律生效）

單位換算（源碼查證 mecanum.py）：duty% = |v| × 100 ；duty% = |wz| × 13.89
服端夾限：|vx|,|vy| ≤ 0.25 m/s（=25% duty）、|wz| ≤ 1.60 rad/s（=22% duty）

安全：每級只動作約 0.5 秒（2 拍），之後**停止心跳** ⇒ 服端 700 ms 內自動歸零解除。

用法:
  python3 drive_test.py fwd            # 前進階梯 20%→100%（占空比 5%→25%）
  python3 drive_test.py fwd 0.6        # 前進固定 60%（占空比 15%）
  python3 drive_test.py bwd|left|right|spinl|spinr [scale]
"""
import json, sys, time, urllib.request

BASE = "http://127.0.0.1:8787"
STAIR = [0.20, 0.40, 0.60, 0.80, 1.00]
BEATS = 2                  # 每拍 ~260 ms ⇒ 約 0.5 秒
GAP = 6.0
LIMITS = {"vx": 0.25, "vy": 0.25, "wz": 1.60}
DUTY = {"linear": 100, "angular": 13.89}
DIRS = {
    "fwd":    dict(vx=+1, vy=0,  wz=0),
    "bwd":    dict(vx=-1, vy=0,  wz=0),
    "left":   dict(vx=0,  vy=-1, wz=0),
    "right":  dict(vx=0,  vy=+1, wz=0),
    "spinl":  dict(vx=0,  vy=0,  wz=-1),
    "spinr":  dict(vx=0,  vy=0,  wz=+1),
}


def post(path, payload=None, timeout=30):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"error": str(e)}


def state():
    try:
        with urllib.request.urlopen(BASE + "/api/state", timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def snap(tag):
    s = state()
    if "error" in s:
        print(f"    [{tag}] 讀狀態失敗"); return
    b = s["topics"].get("/ros_robot_controller/battery", {}).get("last", {})
    d = s["topics"].get("/sonar_controller/get_distance", {}).get("last", {})
    y = s.get("system") or {}
    print(f"    [{tag}] battery={b.get('value')}mV sonar={d.get('value')}mm throttled={y.get('throttled')}")


def pulse(name, direction, scale):
    v = DIRS[direction]
    vx, vy, wz = v["vx"] * LIMITS["vx"] * scale, v["vy"] * LIMITS["vy"] * scale, v["wz"] * LIMITS["wz"] * scale
    d = abs(vx or vy) * DUTY["linear"] or abs(wz) * DUTY["angular"]
    print(f"\n--- {name} scale={scale:.0%} → vx={vx:+.3f} vy={vy:+.3f} wz={wz:+.3f}  ≈ duty {d:.1f}% ---")
    code, arm = post("/api/motion/arm", {})
    if not arm.get("ok"):
        print(f"    武裝失敗 {code}: {arm.get('error')}"); return
    for i in range(BEATS):
        code, r = post("/api/motion/beat", {"vx": vx, "vy": vy, "wz": wz})
        if code != 200:
            print(f"    beat#{i+1} 被拒 {code}: {r.get('error')}"); break
        snap(f"beat{i+1}")
        time.sleep(0.26)
    time.sleep(1.5)
    m = state().get("motion", {})
    print(f"    → armed={m.get('armed')} reason={m.get('reason')}")


def main():
    direction = (sys.argv[1] if len(sys.argv) > 1 else "fwd").lower()
    if direction not in DIRS:
        print(f"方向可用: {', '.join(DIRS)}"); return
    s = state()
    d = s.get("topics", {}).get("/sonar_controller/get_distance", {}).get("last", {}) or {}
    b = s.get("topics", {}).get("/ros_robot_controller/battery", {}).get("last", {}) or {}
    print(f"基準 battery={b.get('value')}mV sonar={d.get('value')}mm throttled={(s.get('system') or {}).get('throttled')}")
    if direction in ("fwd",) and (d.get("value") or 9999) < 250:
        print(f"🔴 前方只有 {d.get('value')} mm，拒絕開始"); return
    if len(sys.argv) > 2:
        pulse(f"{direction} 固定", direction, float(sys.argv[2]))
    else:
        for sc in STAIR:
            pulse(f"{direction} 階梯", direction, sc)
            time.sleep(GAP)
    print("\n結束。")


if __name__ == "__main__":
    main()
