#!/usr/bin/env python3
"""用相機影像的相位相關（phase correlation）客觀判定實際運動方向。

⚠️ **符號已用已知位移的合成圖校準過**（見檔尾 selftest，改這個檔案後請重跑 selftest）：
  `shift()` 回報的 dx **正值 = 場景對畫面往【左】移**，負值 = 往【右】移（與 np.roll 方向相反）
前視相機的幾何關係：
  場景往【左】移 (dx>0)  ⇒  車體往【右】平移，或車頭往【右】偏航
  場景往【右】移 (dx<0)  ⇒  車體往【左】平移，或車頭往【左】偏航
⚠️ 平移與偏航都會造成水平位移、符號相同，故此腳本只能給「往哪邊」的符號，
   要區分二者需靠場景位移隨深度的變化（近物位移大=平移、遠近一致=偏航）或人眼佐證。

用法:  python3 shift_detect.py vy <值> [拍數]
       python3 shift_detect.py wz <值> [拍數]
"""
import asyncio
import io
import json
import sys
import time
import urllib.request

import numpy as np
import websockets
from PIL import Image

SNAP = "http://192.168.149.1:8080/snapshot?topic=/image_raw"


def grab():
    req = urllib.request.Request(SNAP, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def mean_frame(n=6, gap=0.18):
    """取 n 張平均以壓掉 JPEG 雜訊與自動曝光抖動；同時保留最後一張原始 JPEG 供人看"""
    acc = None
    ok = 0
    raw = None
    for _ in range(n):
        try:
            raw = grab()
            g = np.asarray(Image.open(io.BytesIO(raw)).convert("L"), dtype=np.float64)
            acc = g if acc is None else acc + g
            ok += 1
        except Exception as e:
            print(f"    取圖失敗: {type(e).__name__}")
        time.sleep(gap)
    if acc is None:
        raise SystemExit("❌ 連 n 張都抓不到，無法分析")
    return acc / ok, ok, raw


def prep(img):
    h, w = img.shape
    c = img[int(h * 0.25):int(h * 0.95), int(w * 0.08):int(w * 0.92)]   # 中央區，避開邊角暗角
    c = c - c.mean()
    return c * np.hanning(c.shape[0])[:, None] * np.hanning(c.shape[1])[None, :]


def shift(a, b):
    """回傳 (dx, dy, 峰值信心)。dx>0 = 場景對畫面往右移"""
    A, B = np.fft.fft2(prep(a)), np.fft.fft2(prep(b))
    R = A * np.conj(B)
    R /= (np.abs(R) + 1e-9)
    cc = np.fft.ifft2(R).real
    H, W = cc.shape
    iy, ix = np.unravel_index(np.argmax(cc), cc.shape)

    def refine(idx, axis):
        if axis == 0:
            p, q, r = cc[(idx - 1) % H, ix], cc[idx, ix], cc[(idx + 1) % H, ix]
        else:
            p, q, r = cc[iy, (idx - 1) % W], cc[iy, idx], cc[iy, (idx + 1) % W]
        return float(np.clip((p - r) / (2 * (p - 2 * q + r) + 1e-9), -0.5, 0.5))

    dy = (iy - H if iy > H // 2 else iy) + refine(iy, 0)
    dx = (ix - W if ix > W // 2 else ix) + refine(ix, 1)
    # 峰值 / 平均能量：對齊良好時數十～數百倍；去相關時接近 1～3
    conf = float(cc.max() / (np.abs(cc).mean() + 1e-9))
    return dx, dy, conf


async def fire(vy=0.0, wz=0.0, beats=4, span=0.2):
    m = {"linear": {"x": 0.0, "y": float(vy), "z": 0.0},
         "angular": {"x": 0.0, "y": 0.0, "z": float(wz)}}
    z = {"linear": {"x": 0.0, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}
    async with websockets.connect("ws://192.168.149.1:9090", open_timeout=6, ping_interval=None) as ws:
        for _ in range(beats):
            await ws.send(json.dumps({"op": "publish", "topic": "/cmd_vel",
                                      "type": "geometry_msgs/msg/Twist", "msg": m}))
            await asyncio.sleep(span)
        for _ in range(4):                      # 收尾零速（volatile QoS，發 4 次保底）
            await ws.send(json.dumps({"op": "publish", "topic": "/cmd_vel",
                                      "type": "geometry_msgs/msg/Twist", "msg": z}))
            await asyncio.sleep(0.15)


async def main():
    axis = sys.argv[1]
    val = float(sys.argv[2])
    beats = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    vy = val if axis == "vy" else 0.0
    wz = val if axis == "wz" else 0.0
    law = "REP-103: y>0 = 往【左】平移" if axis == "vy" else "REP-103: z>0 = 逆時針 = 車頭往【左】"
    print(f"\n=== {axis} 測試｜{law}｜送值 {val:+.3f}｜{beats} 拍 × 0.2 s ===")

    b, nb, braw = mean_frame()
    print(f"  基準影格 {nb}/6 張（亮度均值 {b.mean():.1f}、標準差 {b.std():.1f} ＝ 場景特徵豐富度）")
    await fire(vy=vy, wz=wz, beats=beats)
    a, na, araw = mean_frame()
    print(f"  事後影格 {na}/6 張")

    dx, dy, conf = shift(b, a)
    tag = f"{axis}_{val:+.2f}"
    open(f"frames/{tag}_before.jpg", "wb").write(braw)
    open(f"frames/{tag}_after.jpg", "wb").write(araw)

    scene = "左" if dx > 0 else "右"      # 場景在畫面的移動方向
    robot = "右" if dx > 0 else "左"      # 車體方向與場景相反
    print(f"\n  影像位移 dx = {dx:+.1f} px（場景往{scene}移 {abs(dx):.0f} px）｜dy = {dy:+.1f} px｜信心 {conf:.0f}")
    print(f"  ⇒ 判讀：車體/車頭 往【{robot}】")
    if conf < 8:
        print("  ⚠️ 峰值信心過低：特徵不足或位移過大到去相關，此結果不可信")
    if abs(dx) < 4 and abs(dy) < 4:
        print("  ⚠️ 位移 < 4 px：可能根本没動（低於雜訊）")
    print(f"  影格: frames/{tag}_before.jpg  frames/{tag}_after.jpg")


def selftest():
    """以已知位移校準 dx/dy 符號；若這個測試不過，所有判讀都不可信。"""
    base = np.random.default_rng(7).random((240, 320)) * 255
    ok = True
    for n, want in ((20, "右"), (-20, "左")):
        dx, _, _ = shift(base, np.roll(base, n, axis=1))
        got = "右" if dx < 0 else "左"          # 本檔的符號約定向：dx<0 = 場景往右
        flag = "OK" if got == want else "❌ 符號跑了"
        ok &= got == want
        print(f"  [selftest] 真值往{want}移 {abs(n)} px → dx={dx:+.1f} → 判讀往{got}  {flag}")
    return ok


if __name__ == "__main__":
    if not selftest():
        raise SystemExit("❌ 符號自檢失敗，拒絕輸出判讀結果")
    asyncio.run(main())
