#!/usr/bin/env python3
"""雲台左右掃視並拍照。用已建立的 rosbridge WebSocket 發布，offset 一律送空陣列（非空會觸發 set_offset）。"""
import asyncio, json, sys, urllib.request, websockets
TOPIC = "/ros_robot_controller/pwm_servo/set_state"
SVC   = "/ros_robot_controller/pwm_servo/get_state"
SNAP  = "http://192.168.149.1:8080/snapshot?topic=/image_raw"

def shot(path):
    req = urllib.request.Request(SNAP, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
        d = r.read(); f.write(d)
    return len(d)

async def readback(ws, sid=2):
    """讀兩次並比對（厂家 sdk 不會把回覆對到請求，必讀兩遍）"""
    vals = []
    for _ in range(2):
        await ws.send(json.dumps({"op":"call_service","service":SVC,
            "args":{"cmd":[{"id":sid,"get_position":1,"get_offset":1}]}}))
        while True:
            d = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if d.get("op")=="service_response" and d.get("service")==SVC:
                st = (d.get("values") or {}).get("state") or [{}]
                vals.append(st[0].get("position")); break
    return vals

async def main():
    targets = [int(x) for x in sys.argv[1:]] or [1100, 1900, 1500]
    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        for pos in targets:
            for _ in range(3):   # volatile QoS：連發三次保底送達
                await ws.send(json.dumps({"op":"publish","topic":TOPIC,
                    "type":"ros_robot_controller_msgs/msg/SetPWMServoState",
                    "msg":{"state":[{"id":[2],"position":[pos],"offset":[]}],"duration":0.6}}))
                await asyncio.sleep(0.15)
            await asyncio.sleep(1.6)
            rb = await readback(ws)
            ok = "✅" if rb[0] == rb[1] == pos else f"⚠️ 回讀 {rb}"
            p = f"frames/pan_{pos}.jpg"
            try: n = shot(p); img = f"影格 {n/1024:.0f} KB → {p}"
            except Exception as e: img = f"影格失敗 {type(e).__name__}"
            print(f"  pan → {pos} µs   回讀 {rb}  {ok}   {img}")
            await asyncio.sleep(0.6)
asyncio.run(main())
