#!/usr/bin/env python3
"""單軸方向驗證：經 webui 閘道（護欄生效）發脈衝，前後取影格並用 selftest 校準過的相位相關判讀。
   用法: axis_test.py vy|-0.25|2      （軸 | 值 | 拍數，每拍 0.2 s）"""
import asyncio, importlib.util, io, json, statistics, sys, time, urllib.request
import numpy as np
from PIL import Image

BASE = "http://127.0.0.1:8787"
SNAP = "http://192.168.149.1:8080/snapshot?topic=/image_raw"
spec = importlib.util.spec_from_file_location("sd", "shift_detect.py")
sd = importlib.util.module_from_spec(spec); spec.loader.exec_module(sd)

def api(path, payload=None):
    req = urllib.request.Request(BASE+path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
    except Exception as e: return 0, {"error": str(e)}

def frames(n=4):
    accs, raw = [], None
    for _ in range(n):
        try:
            req = urllib.request.Request(SNAP, headers={"User-Agent":"curl/8"})
            with urllib.request.urlopen(req, timeout=25) as r: raw = r.read()
            accs.append(np.asarray(Image.open(io.BytesIO(raw)).convert("L"), dtype=np.float64))
        except Exception: pass
        time.sleep(0.16)
    return (np.mean(accs, axis=0), raw) if accs else (None, None)

async def main():
    axis, val, beats = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
    st, s = api("/api/state", {})
    m = (s.get("motion") or {})
    print(f"  事前狀態: estopped={m.get('estopped')} armed={m.get('armed')} sonar={s['topics']['/sonar_controller/get_distance']['last']['value']}mm")
    if m.get("estopped"): print("  ❌ 急停中，中止"); return
    b, braw = frames()
    print(f"  基準影格 ok={b is not None}")
    await asyncio.sleep(0.4)
    code, body = api("/api/motion/arm", {})
    print(f"  arm → HTTP {code} {body}")
    if code != 200: print("  ❌ 武裝失敗，中止"); return
    v = {"vx":0.0,"vy":0.0,"wz":0.0}; v[axis] = val
    for i in range(beats):
        c2, b2 = api("/api/motion/beat", v)
        print(f"    beat{i+1} → HTTP {c2} duty={b2.get('duty')}")
        await asyncio.sleep(0.2)
    print("  停止心跳，等伺服端看門狗自動歸零…")
    await asyncio.sleep(2.2)
    st2, s2 = api("/api/state", {})
    print(f"  事後狀態: armed={s2['motion']['armed']} reason={s2['motion'].get('reason')} "
          f"sonar={s2['topics']['/sonar_controller/get_distance']['last']['value']}mm")
    a, araw = frames()
    tag = f"{axis}{val:+.2f}_{int(time.time())}"
    if b is None or a is None: print("  ❌ 影格缺失"); return
    Image.open(io.BytesIO(braw)).save(f"frames/{tag}_before.jpg")
    Image.open(io.BytesIO(araw)).save(f"frames/{tag}_after.jpg")
    dx, dy, conf = sd.shift(b, a)
    scene = "左" if dx > 0 else "右"; robot = "右" if dx > 0 else "左"
    H = b.shape[0]
    def band(r0, r1):
        ddx, _, cc = sd.shift(b[int(H*r0):int(H*r1),:], a[int(H*r0):int(H*r1),:])
        return ddx, cc
    near, nearc = band(0.60, 0.98); far, farc = band(0.05, 0.45)
    print(f"\n  全圖 dx={dx:+.1f}px（場景往{scene}移）→ 車體往【{robot}】  信心={conf:.0f}")
    print(f"  近景帶 dx={near:+.1f}px（信心{nearc:.0f}）｜遠景帶 dx={far:+.1f}px（信心{farc:.0f}）")
    ratio = abs(near)/max(abs(far),0.5)
    print(f"  近/遠位移比 = {ratio:.2f}  {'⇒ 近物位移明顯較大 → 偏向平移' if ratio>1.35 else '⇒ 遠近一致 → 偏向偏航'}")
    if abs(dx) < 4: print("  ⚠️ |dx|<4px：可能沒動")
    if conf < 8: print("  ⚠️ 信心偏低：場景去相關（位移過大或特徵不足），數值不可信，請以肉眼為準")
    print(f"  影格: frames/{tag}_before.jpg / _after.jpg")

asyncio.run(main())
