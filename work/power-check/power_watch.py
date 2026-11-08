#!/usr/bin/env python3
"""
電力儀表板（跑在桌機上，量測負擔不落在 Pi）

- 經 rosbridge(WebSocket) 以 ~2 Hz 讀 /ros_robot_controller/battery
- 經 ssh 每 5 秒讀 vcgencmd get_throttled / measure_volts / measure_temp + load
- 即時偵測 brownout（throttled bit0 = undervoltage detected）並大声示警
- 寫 CSV，事後可看電壓墬陷幅度

用法（Pi 開機、桌機 wlo1 已接回 AP 之後）:
    python3 power_watch.py                    # 預設 600 秒
    python3 power_watch.py 120
    python3 power_watch.py 300 --host pi@192.168.100.21   # 走家網那條也行

結束時印出 battery 的最小/最大值與 throttled 判定。
"""
import asyncio, csv, json, os, subprocess, sys, time
import websockets

KEY = os.path.expanduser("~/.ssh/id_ed25519")

def parse_args():
    args = sys.argv[1:]
    dur = 600
    if args and args[0].isdigit():
        dur = int(args.pop(0))
    host = "pi@192.168.149.1"
    if "--host" in args and len(args) > args.index("--host") + 1:
        host = args[args.index("--host") + 1]
    return dur, host


def system_snapshot(host):
    remote = ("vcgencmd get_throttled; vcgencmd measure_volts; "
              "vcgencmd measure_temp; cut -d' ' -f1 /proc/loadavg")
    cmd = ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, remote]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.splitlines()
    except Exception as e:
        return f"ERR:{type(e).__name__}", "NA", "NA", "NA"
    def val(i, sep="="):
        return out[i].split(sep)[-1].strip() if len(out) > i else "NA"
    return val(0), val(1), val(2), val(3, " ")


def verdict(vals, last_thr):
    print("\n=== 總結 ===")
    if vals:
        print(f"  battery 原始值: min={min(vals)} max={max(vals)} 末值={vals[-1]} n={len(vals)}")
        lo = min(vals)
        if lo > 6000:
            print("  判讀: >6000 ⇒ 這個欄位量的是 2S 電池總壓(mV)。滿電約 8400、建議下限約 6600")
        elif 4300 <= lo <= 5400:
            print("  判讀: ≈4.3-5.4V ⇒ 這個欄位量的是 5V 電源軌，不是電池 → 電量仍不可知")
        else:
            print("  判讀: 數值異常,需人工比對(用三用電表量電池總壓)")
    else:
        print("  沒收到任何 battery 資料(ROS 沒起來? 或 rosbridge 沒通?)")
    try:
        bits = int(last_thr, 16)
    except Exception:
        bits = None
    if bits is None:
        print(f"  throttled: 讀取失敗({last_thr})")
    elif bits == 0:
        print(f"  throttled = {last_thr} ⇒ ✅ 整段過程中從未發生供電不足")
    else:
        flags = []
        for b, name in ((0x1, "undervoltage detected"), (0x2, "arm frequency capped"),
                        (0x4, "currently throttled"), (0x8, "soft temperature limit active"),
                        (0x10000, "under-voltage has occurred"), (0x20000, "frequency cap has occurred"),
                        (0x40000, "throttling has occurred"), (0x80000, "soft temp limit has occurred")):
            if bits & b:
                flags.append(f"0x{b:x} {name}")
        print(f"  throttled = {last_thr} ⇒ 🔴 {', '.join(flags)}")


async def main():
    dur, host = parse_args()
    ip = host.split("@")[-1]
    url = f"ws://{ip}:9090"
    fn = f"power_log_{time.strftime('%Y%m%d-%H%M%S')}.csv"
    f = open(fn, "w", newline="")
    w = csv.writer(f)
    w.writerow(["t_s", "battery_raw", "throttled", "core_volts", "temp", "load1", "note"])
    print(f"記錄 → {fn}（{dur} 秒）  host={host}")
    print("提示: bit0=1（undervoltage）會立刻顯示 ⚠️ — 那就是供電不足的即時證據\n")

    vals, last_thr, note = [], None, ""
    t0 = time.time()
    last_sys, sys_row = 0.0, ("NA", "NA", "NA", "NA")

    try:
        async with websockets.connect(url, open_timeout=8, ping_interval=None) as ws:
            await ws.send(json.dumps({"op": "subscribe", "topic": "/ros_robot_controller/battery",
                                      "type": "std_msgs/msg/UInt16", "id": "p"}))
            while time.time() - t0 < dur:
                try:
                    d = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                except asyncio.TimeoutError:
                    print("  … rosbridge 逾時（Pi 可能重啟或 AP 斷線）")
                    continue
                if d.get("op") != "publish":
                    continue
                v = d["msg"]["data"]
                vals.append(v)
                now = time.time()
                if now - last_sys >= 5:
                    sys_row = await asyncio.get_event_loop().run_in_executor(None, system_snapshot, host)
                    last_sys = now
                thr, volts, temp, load = sys_row
                note = ""
                try:
                    if int(thr, 16) & 1:
                        note = "⚠️⚠️ UNDERVOLTAGE（供電不足）"
                except Exception:
                    pass
                if last_thr is not None and thr != last_thr:
                    print(f"  🔺 throttled {last_thr} → {thr}  {note}")
                last_thr = thr
                if note:
                    print(f"  {note} battery={v} core={volts} temp={temp} load={load}")
                w.writerow([round(now - t0), v, thr, volts, temp, load, note])
                f.flush()
    except Exception as e:
        print(f"  連線失敗: {type(e).__name__} {e}")
    finally:
        f.close()
        verdict(vals, last_thr)


if __name__ == "__main__":
    asyncio.run(main())
