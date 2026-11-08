#!/usr/bin/env python3
"""啟動門檻階梯：每個占空比做「前進 0.5s → 後退 0.5s」，用超音波中位數當客觀位移計。"""
import asyncio, json, statistics, urllib.request, websockets
LEVELS = [0.16, 0.18, 0.20, 0.22, 0.25]     # m/s，數值即占空比 %
BASE = "http://127.0.0.1:8787"

def api(path, payload=None, timeout=40):
    req = urllib.request.Request(BASE+path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
    except Exception as e: return 0, {"error": str(e)}

async def sonar(n=6):
    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        await ws.send(json.dumps({"op":"subscribe","topic":"/sonar_controller/get_distance","type":"std_msgs/msg/Int32"}))
        v=[]
        while len(v) < n:
            d=json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if d.get("op")=="publish": v.append(d["msg"]["data"])
        return statistics.median(v)

async def pulse(vx, beats=2):
    api("/api/motion/arm", {})
    for _ in range(beats):
        api("/api/motion/beat", {"vx":vx, "vy":0, "wz":0}); await asyncio.sleep(0.26)
    await asyncio.sleep(1.4)                      # 等看門狗歸零解除
    return api("/api/state")[1] if False else None

async def main():
    print(f"{'占空比':>6} {'前位移':>9} {'後位移':>9}  判定")
    for lv in LEVELS:
        s0 = await sonar()
        await pulse(+lv); await asyncio.sleep(0.4)
        s1 = await sonar()
        await pulse(-lv); await asyncio.sleep(0.4)
        s2 = await sonar()
        d_fwd, d_back = s0 - s1, s2 - s1          # 正值 = 前進（障礙物變近）
        moved = abs(d_fwd) >= 25
        print(f"{lv*100:5.0f}% {d_fwd:+8.0f}mm {d_back:+8.0f}mm  {'✅ 會走' if moved else '— 沒動'}   (sonar {s0}→{s1}→{s2})")
        await asyncio.sleep(2.0)
    print("\n結束。若某級開始出現位移，該級即為啟動門檻。")
asyncio.run(main())
