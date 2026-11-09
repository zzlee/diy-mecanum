#!/usr/bin/env python3
"""capture B：站點式拍攝（停穩才拍）＋站點內 pan 掃三檔，製造分散的視圖給 SfM。
   全程經 webui 閘道（護欄生效）；聲納 <320 mm 緊急停止。"""
import asyncio
import csv
import json
import subprocess
import time
import urllib.request

import websockets

BASE = "http://127.0.0.1:8787"
STREAM = "http://192.168.149.1:8080/stream?topic=/image_raw"
PANT = "/ros_robot_controller/pwm_servo/set_state"
OUT = "capture/runB.mp4"
CSVOUT = "capture/runB_telemetry.csv"
REC = 66.0
VX = 0.22
WZ = 1.6
STOP_MM = 320
PANS = (1300, 1500, 1700)


def api(path, payload=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"error": str(e)}


async def sonar_feed(stop, latest):
    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        await ws.send(json.dumps({"op": "subscribe", "topic": "/sonar_controller/get_distance",
                                  "type": "std_msgs/msg/Int32", "throttle_rate": 50}))
        await ws.send(json.dumps({"op": "subscribe", "topic": "/ros_robot_controller/battery",
                                  "type": "std_msgs/msg/UInt16", "throttle_rate": 200}))
        while not stop.is_set():
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                continue
            if m.get("op") == "publish":
                if "sonar" in m["topic"]:
                    latest["mm"] = m["msg"]["data"]
                else:
                    latest["mv"] = m["msg"]["data"]


async def pan(ws, pos):
    for _ in range(3):
        await ws.send(json.dumps({"op": "publish", "topic": PANT,
                                  "type": "ros_robot_controller_msgs/msg/SetPWMServoState",
                                  "msg": {"state": [{"id": [2], "position": [pos], "offset": []}],
                                          "duration": 0.6}}))
        await asyncio.sleep(0.12)
    await asyncio.sleep(1.4)          # 等舵機到位並穩定


async def station(ws, name, rows, t0, latest):
    for p in PANS:
        await pan(ws, p)
        rows.append((round(time.time() - t0, 2), latest.get("mm"), latest.get("mv"), f"{name}_pan{p}"))
        print(f"    {name} pan={p}  sonar={latest.get('mm')}mm")
        await asyncio.sleep(0.8)      # 每個 pan 位置再停一下，讓影格清晰


async def move(name, rows, t0, latest, vx=0.0, wz=0.0, beats=7):
    c, b = api("/api/motion/arm", {})
    if c != 200:
        print(f"    ❌ {name} arm 失敗 {c} {b}")
        return False
    for i in range(beats):
        if vx > 0 and latest.get("mm", 9999) < STOP_MM:
            print(f"    🔴 緊急停止：聲納 {latest['mm']} mm < {STOP_MM}（{name} 第 {i+1} 拍）")
            api("/api/motion/stop", {})
            return False
        c2, b2 = api("/api/motion/beat", {"vx": vx, "vy": 0.0, "wz": wz})
        if c2 != 200:
            print(f"    ❌ {name} beat 被拒 {c2} {b2}")
            return False
        rows.append((round(time.time() - t0, 2), latest.get("mm"), latest.get("mv"), f"{name}_b{i+1}"))
        await asyncio.sleep(0.2)
    await asyncio.sleep(2.4)          # 看門狗歸零 + 車停穩
    return True


async def main():
    latest = {}
    stop = asyncio.Event()
    rows = []
    rec = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-rw_timeout", "8000000",
                            "-i", STREAM, "-t", str(REC), "-an", "-r", "30", "-c:v", "libx264",
                            "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p", OUT],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  錄影啟動 → {OUT}（{REC:.0f} 秒）")
    await asyncio.sleep(0.8)
    task = asyncio.create_task(sonar_feed(stop, latest))
    await asyncio.sleep(0.4)
    t0 = time.time()
    print(f"  起點：聲納 {latest.get('mm')} mm｜電池 {latest.get('mv')} mV")

    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        print("\n  [S1] 起站");      await station(ws, "S1", rows, t0, latest)
        print("\n  [前進 1.4 s]")
        ok1 = await move("前進", rows, t0, latest, vx=VX, beats=7)
        print(f"    → 聲納 {latest.get('mm')} mm")
        if ok1:
            print("\n  [S2]");       await station(ws, "S2", rows, t0, latest)
            print("\n  [原地轉 1.0 s]")
            ok2 = await move("旋轉", rows, t0, latest, wz=WZ, beats=5)
            print(f"    → 聲納 {latest.get('mm')} mm")
            print("\n  [S3]");       await station(ws, "S3", rows, t0, latest)
            print("\n  [前進 1.2 s]")
            ok3 = await move("前進2", rows, t0, latest, vx=VX, beats=6)
            print(f"    → 聲納 {latest.get('mm')} mm")
            print("\n  [S4]");       await station(ws, "S4", rows, t0, latest)
        print("\n  [後退 1.4 s 撤回]")
        await move("後退", rows, t0, latest, vx=-VX, beats=7)
        print("\n  [S5] 終站");      await station(ws, "S5", rows, t0, latest)
        # 雲台歸中
        await pan(ws, 1500)

    stop.set()
    task.cancel()
    mm = [r[1] for r in rows if r[1]]
    print(f"\n  聲納軌跡：起 {mm[0]}｜最小 {min(mm)}｜末 {mm[-1]} mm")
    print(f"  電池 {rows[0][2]} → {rows[-1][2]} mV")
    with open(CSVOUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "sonar_mm", "battery_mv", "event"])
        w.writerows(rows)
    print(f"  事件 {len(rows)} 筆 → {CSVOUT}")
    try:
        rec.wait(timeout=45)
        print(f"  錄影結束 rc={rec.returncode}")
    except subprocess.TimeoutExpired:
        rec.terminate()
        print("  錄影以 terminate 收尾（檔案通常仍完整，稍後用 ffprobe 驗）")


asyncio.run(main())
