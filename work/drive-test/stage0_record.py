#!/usr/bin/env python3
"""Stage 0：邊錄影邊慢速前進/後退，產出可供 SfM 判定的影片＋遙測軌跡。
   安全：聲納 <320 mm 立即緊急停止；心跳看門狗在伺服器端。"""
import asyncio, csv, json, subprocess, time, urllib.request, websockets
BASE="http://127.0.0.1:8787"; STREAM="http://192.168.149.1:8080/stream?topic=/image_raw"
OUT="capture/runA.mp4"; CSVOUT="capture/runA_telemetry.csv"
VX=0.22; STOP_MM=320; REC=14.0
def api(path, payload=None):
    req=urllib.request.Request(BASE+path, data=json.dumps(payload or {}).encode(),
                               headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
    except Exception as e: return 0, {"error": str(e)}
async def sonar_feed(stop, latest):
    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        await ws.send(json.dumps({"op":"subscribe","topic":"/sonar_controller/get_distance","type":"std_msgs/msg/Int32","throttle_rate":50}))
        await ws.send(json.dumps({"op":"subscribe","topic":"/ros_robot_controller/battery","type":"std_msgs/msg/UInt16","throttle_rate":200}))
        while not stop.is_set():
            try: m=json.loads(await asyncio.wait_for(ws.recv(),timeout=2))
            except asyncio.TimeoutError: continue
            if m.get("op")=="publish":
                if "sonar" in m["topic"]: latest["mm"]=m["msg"]["data"]
                else: latest["mv"]=m["msg"]["data"]
async def phase(name, vx, beats, latest, rows, t0):
    c,b=api("/api/motion/arm",{})
    if c!=200: print(f"    ❌ {name} arm 失敗 {c} {b}"); return False
    for i in range(beats):
        if latest.get("mm",9999) < STOP_MM:
            print(f"    🔴 緊急停止：聲納 {latest['mm']} mm < {STOP_MM}（{name} 第 {i+1} 拍）")
            api("/api/motion/stop",{}); return False
        c2,b2=api("/api/motion/beat",{"vx":vx,"vy":0.0,"wz":0.0})
        if c2!=200: print(f"    ❌ {name} beat 被拒 {c2} {b2}"); return False
        rows.append((round(time.time()-t0,2), latest.get("mm"), latest.get("mv"), name))
        await asyncio.sleep(0.2)
    await asyncio.sleep(2.2)   # 看門狗歸零
    return True
async def main():
    latest={}; stop=asyncio.Event(); rows=[]
    rec=subprocess.Popen(["ffmpeg","-hide_banner","-loglevel","error","-rw_timeout","8000000","-i",STREAM,
                          "-t",str(REC),"-an","-r","30","-c:v","libx264","-preset","ultrafast","-crf","16",
                          "-pix_fmt","yuv420p",OUT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  錄影啟動 → {OUT}（{REC:.0f} 秒）")
    await asyncio.sleep(0.8)
    t0=time.time()
    task=asyncio.create_task(sonar_feed(stop, latest))
    await asyncio.sleep(0.3)
    print(f"  起點聲納 {latest.get('mm')} mm｜電池 {latest.get('mv')} mV")
    print("  靜止 1.5 秒…"); await asyncio.sleep(1.5)
    ok1=await phase("前進", +VX, 7, latest, rows, t0)
    print(f"  前進結束，聲納 {latest.get('mm')} mm")
    await asyncio.sleep(1.2)
    if ok1:
        ok2=await phase("後退", -VX, 7, latest, rows, t0)
        print(f"  後退結束，聲納 {latest.get('mm')} mm")
    stop.set(); task.cancel()
    mm=[r[1] for r in rows if r[1]]; 
    print(f"\n  聲納軌跡: 起 {mm[0]} mm → 最小 {min(mm)} mm → 末 {mm[-1]} mm")
    print(f"  ⇒ 有效基線（去程位移）≈ {mm[0]-min(mm)} mm")
    with open(CSVOUT,"w",newline="") as f:
        w=csv.writer(f); w.writerow(["t_s","sonar_mm","battery_mv","phase"]); w.writerows(rows)
    print(f"  遙測 {len(rows)} 筆 → {CSVOUT}")
    rec.wait(timeout=40)
    print(f"  錄影結束 rc={rec.returncode}")
asyncio.run(main())
