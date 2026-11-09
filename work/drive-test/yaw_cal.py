#!/usr/bin/env python3
"""角速度標定：定 wz 發固定時長，用相機影像位移換算實際角度（fx=473.451）。
   每檔正向測完立刻反向轉回，避免累積偏移。用法: yaw_cal.py [beat數=3]"""
import asyncio, importlib.util, io, json, math, sys, time, urllib.request
import numpy as np
from PIL import Image

BASE, SNAP, FX = "http://127.0.0.1:8787", "http://192.168.149.1:8080/snapshot?topic=/image_raw", 473.451
spec = importlib.util.spec_from_file_location("sd", "shift_detect.py")
sd = importlib.util.module_from_spec(spec); spec.loader.exec_module(sd)
BEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
WZ = [1.0, 1.44, 1.80]

def api(path, payload=None):
    req = urllib.request.Request(BASE+path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
    except Exception as e: return 0, {"error": str(e)}

def frames(n=4):
    acc = []
    for _ in range(n):
        try:
            req = urllib.request.Request(SNAP, headers={"User-Agent":"curl/8"})
            with urllib.request.urlopen(req, timeout=25) as r:
                acc.append(np.asarray(Image.open(io.BytesIO(r.read())).convert("L"), dtype=np.float64))
        except Exception: pass
        time.sleep(0.16)
    return np.mean(acc, axis=0) if acc else None

def batt():
    _, s = api("/api/state", {})
    return s["topics"]["/ros_robot_controller/battery"]["last"]["value"], s["system"]["throttled"]

async def run(z):
    """取影格要在 arm 之前完成：arm 後若 700 ms 內沒 beat，伺服器會自動解除武裝（心跳看門狗）"""
    dur = BEATS * 0.2
    c, b = api("/api/motion/arm", {})
    if c != 200:
        print(f"    ❌ arm 失敗 HTTP {c} {b}"); return None
    for i in range(BEATS):
        c2, b2 = api("/api/motion/beat", {"vx":0.0,"vy":0.0,"wz":z})
        if c2 != 200:
            print(f"    ❌ beat{i+1} 被拒 HTTP {c2} {b2} → 中止本檔"); return None
        await asyncio.sleep(0.2)
    await asyncio.sleep(2.2)                      # 等看門狗自動歸零
    return dur

async def main():
    if not sd.selftest():
        print("❌ 符號自檢失敗"); return
    b0, th0 = batt()
    print(f"  起點電量 {b0} mV｜throttled {th0}｜每檔 {BEATS} 拍 = {BEATS*0.2:.1f} 秒\n")
    print(f"  {'wz':>6} {'dx(px)':>8} {'實測角度':>9} {'實際 rad/s':>11} {'命令/實際':>10}  信心  近/遠帶  方向")
    code, body = api("/api/motion/arm", {})
    if code != 200: print("  ❌ 武裝失敗", body); return
    for z in WZ:
        b = frames()                              # 先取影格，避免 arm 後空窗
        dur = await run(z)
        if dur is None: print(f"  wz={z} 未完成，跳過"); continue
        a = frames()
        dx, dy, conf = sd.shift(b, a)
        ang = math.degrees(math.atan(abs(dx)/FX))
        rate = math.radians(ang)/dur
        H = b.shape[0]
        near, _ = sd.shift(b[int(H*0.60):int(H*0.98),:], a[int(H*0.60):int(H*0.98),:])[:2]
        far,  _ = sd.shift(b[int(H*0.05):int(H*0.45),:], a[int(H*0.05):int(H*0.45),:])[:2]
        ratio = abs(near)/max(abs(far), 0.5)
        side = "左" if dx < 0 else "右"           # dx<0 = 場景右移 = 車頭往左
        flag = "" if conf >= 8 else "  ⚠️信心低"
        print(f"  {z:6.2f} {dx:8.1f} {ang:8.1f}° {rate:11.3f} {z/max(rate,1e-6):10.2f}  {conf:5.0f}  {ratio:4.2f}   車頭往{side}{flag}")
        await asyncio.sleep(1.0)
        b2 = frames()                              # 反向轉回
        d2 = await run(-z)
        if d2 is None: print("  反向未完成"); continue
        a2 = frames()
        dx2, _, c2 = sd.shift(b2, a2)
        print(f"       └ 反向 {abs(z):.2f} 轉回：dx={dx2:+.1f}px（應與上列反號）信心 {c2:.0f}")
        await asyncio.sleep(1.5)
    b1, th1 = batt()
    print(f"\n  終點電量 {b1} mV｜throttled {th1}")
    print("  📌 「命令/實際」= 厂家宣告值與實測的增益比；>1 代表實際比命令慢")
    print("  📌 近/遠帶比 ≈1 ⇒ 純偏航（本測試預期值）；>1.35 ⇒ 摻雜平移，數值需重看")

asyncio.run(main())
