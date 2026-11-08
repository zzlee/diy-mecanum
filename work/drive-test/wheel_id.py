#!/usr/bin/env python3
"""逐輪單獨測試：直接發布 set_motor_speeds（繞開運動學，一次只給一個馬達 duty）"""
import asyncio, json, websockets
DUTY = 25
async def main():
    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        for mid in (1, 2, 3, 4):
            print(f"\n>>> 只有馬達 ID {mid}  →  duty {DUTY}%  （約 0.6 秒）")
            for _ in range(3):
                await ws.send(json.dumps({"op":"publish","topic":"/ros_robot_controller/set_motor_speeds",
                    "type":"ros_robot_controller_msgs/msg/MotorsSpeedControl",
                    "msg":{"data":[{"id":mid,"speed":float(DUTY)}]}}))
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.4)
            for _ in range(3):
                await ws.send(json.dumps({"op":"publish","topic":"/ros_robot_controller/set_motor_speeds",
                    "type":"ros_robot_controller_msgs/msg/MotorsSpeedControl",
                    "msg":{"data":[{"id":mid,"speed":0.0}]}}))
                await asyncio.sleep(0.15)
            print("    已歸零，等 3 秒…")
            await asyncio.sleep(3.0)
        print("\n結束（全部已歸零）")
asyncio.run(main())
