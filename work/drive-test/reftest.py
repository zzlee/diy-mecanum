#!/usr/bin/env python3
"""直接發 /cmd_vel（不经 web app），量出厂家對 y / z 的符號慣例。"""
import asyncio, json, websockets
async def fire(ws, label, vec, beats=4):
    print(f"\n>>> {label}   payload={vec}")
    m={"linear":{"x":vec.get("vx",0),"y":vec.get("vy",0),"z":0},
       "angular":{"x":0,"y":0,"z":vec.get("wz",0)}}
    for _ in range(beats):
        await ws.send(json.dumps({"op":"publish","topic":"/cmd_vel","type":"geometry_msgs/msg/Twist","msg":m}))
        await asyncio.sleep(0.2)
    z={"linear":{"x":0,"y":0,"z":0},"angular":{"x":0,"y":0,"z":0}}
    for _ in range(4):
        await ws.send(json.dumps({"op":"publish","topic":"/cmd_vel","type":"geometry_msgs/msg/Twist","msg":z}))
        await asyncio.sleep(0.15)
    print("    已歸零，等 4 秒…")
    await asyncio.sleep(4.0)
async def main():
    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        await fire(ws, "測試A：linear.y = +0.22（REP-103 定義：正值 = 往【左】）", {"vy":0.22})
        await fire(ws, "測試B：angular.z = +1.44（REP-103 定義：正值 = 逆時針 = 車頭往【左】）", {"wz":1.44})
        print("\n結束。請回報：A 車往哪邊滑？ B 車頭往哪邊轉？")
asyncio.run(main())
