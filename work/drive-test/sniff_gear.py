#!/usr/bin/env python3
"""邊發 cmd_vel 邊監聽 /ros_robot_controller/set_motor_speeds，看實際抵達節點的數值。"""
import asyncio, json, urllib.request, websockets
CMD=0.15   # vx
async def beat(n):
    for _ in range(n):
        try:
            r=urllib.request.Request("http://127.0.0.1:8787/api/motion/beat",
                data=json.dumps({"vx":CMD,"vy":0,"wz":0}).encode(),
                headers={"Content-Type":"application/json"},method="POST")
            urllib.request.urlopen(r,timeout=10).read()
        except Exception as e: print("  beat 失敗",e)
        await asyncio.sleep(0.28)
async def arm():
    r=urllib.request.Request("http://127.0.0.1:8787/api/motion/arm",data=b"{}",
        headers={"Content-Type":"application/json"},method="POST")
    print("  arm:",urllib.request.urlopen(r,timeout=40).read().decode()[:80])
async def stop():
    r=urllib.request.Request("http://127.0.0.1:8787/api/motion/stop",
        data=json.dumps({"reason":"sniff 結束"}).encode(),
        headers={"Content-Type":"application/json"},method="POST")
    urllib.request.urlopen(r,timeout=15).read()
async def main():
    async with websockets.connect("ws://192.168.149.1:9090",open_timeout=6,ping_interval=None) as ws:
        await ws.send(json.dumps({"op":"subscribe","topic":"/ros_robot_controller/set_motor_speeds",
                                  "type":"ros_robot_controller_msgs/msg/MotorsSpeedControl"}))
        await ws.send(json.dumps({"op":"subscribe","topic":"/cmd_vel","type":"geometry_msgs/msg/Twist"}))
        await asyncio.sleep(1.0)
        await arm()
        await asyncio.gather(beat(8), collect(ws))
        await stop()
async def collect(ws):
    got=[]
    while True:
        try: d=json.loads(await asyncio.wait_for(ws.recv(),timeout=6))
        except asyncio.TimeoutError: break
        if d.get("op")!="publish": continue
        if d["topic"].endswith("set_motor_speeds"):
            got.append(d["msg"])
            ds=[(x["id"],round(x["speed"],3)) for x in d["msg"].get("data",[])]
            print(f"  → set_motor_speeds: {ds}")
        elif d["topic"]=="/cmd_vel" and len(got)==0:
            print(f"  · cmd_vel: linear.x={d['msg']['linear']['x']}")
    print(f"  共收到 {len(got)} 筆 set_motor_speeds")
asyncio.run(main())
