#!/usr/bin/env python3
"""匯出檢視器資料（pycolmap 4.2.1 實測 API）：
   點雲含顏色 + 每張影格姿態（quat/光心/視向）+ 該影格可見的點索引，並附符號自查。
   用法：SFM_DATA=/data/suchen/3dscan/data/runA python -u export_viewer.py"""
import json
import os
import pathlib

import numpy as np
import pycolmap

S = 0.336 / 9.957                      # 聲納錨定：1 COLMAP 單位 = 33.7 mm
D = pathlib.Path(os.environ.get("SFM_DATA", "/data/suchen/3dscan/data/runA"))
MODEL, TH = D / "sparse" / "0", D / "thumbs"
FX, FY, CX, CY, W, H = 473.451, 474.217, 323.551, 238.602, 640, 480

rec = pycolmap.Reconstruction(str(MODEL))
imgs = list(rec.images.values()) if hasattr(rec.images, "values") else list(rec.images())
REG = set(int(i) for i in rec.reg_image_ids())
_p3 = list(rec.points3D.items()) if hasattr(rec.points3D, "items") else list(enumerate(rec.points3D()))
P3 = [p for _, p in _p3]
pid2idx = {k: i for i, (k, p) in enumerate(_p3)}
pts = np.asarray([[float(v) for v in p.xyz] for p in P3])
rgb = [[int(v) for v in p.color] if getattr(p, "color", None) is not None else [200, 200, 200] for p in P3]
print(f"點 {len(P3)}｜已註冊 {len(REG)} 張")

cams = []
for k, im in enumerate(imgs):
    if int(im.image_id) not in REG:
        continue
    cf = im.cam_from_world()                       # 這個 build 裡是方法
    R = np.asarray(cf.rotation.matrix(), dtype=float)          # world → cam
    C = np.asarray(im.projection_center(), dtype=float)         # 光心（不是 cf.translation！那是 t）
    q = cf.rotation.quat
    q = np.asarray(q() if callable(q) else q, dtype=float)
    vd = np.asarray(im.viewing_direction(), dtype=float)        # 光軸在世界座標的方向
    p2 = im.points2D.values() if hasattr(im.points2D, "values") else list(im.points2D)
    if k == 0:
        print("Point2D 可用:", [a for a in dir(p2[0]) if not a.startswith("_")][:8])
    ids = [getattr(o, "point3D_id", -1) for o in p2]
    vis = [pid2idx[i] for i in ids if i in pid2idx]
    cams.append({"name": im.name, "center": C, "quat_wxyz": q, "view_dir": vd, "R": R, "vis": vis})

# ---- 符號自查：可見點換到相機座標 ⇒ z 中位應為正且 ~0.1–3 m；光軸應等於 R^T·[0,0,1] ----
print("\n=== 符號自查 ===")
bad = 0
for c in cams[:5]:
    z = ((pts[c["vis"]] - c["center"]) @ c["R"].T)[:, 2]
    ax = c["R"].T @ np.array([0.0, 0.0, 1.0])
    ang = np.degrees(np.arccos(np.clip(np.dot(ax / np.linalg.norm(ax), c["view_dir"] / np.linalg.norm(c["view_dir"])), -1, 1)))
    zm = float(np.median(z)) * S          # 門檻是「公尺」，別拿 COLMAP 單位去比
    ok = 0.05 < zm < 3.0 and ang < 2.0
    bad += 0 if ok else 1
    print(f"  {c['name']}: 可見 {len(c['vis'])} 點｜z 中位 {np.median(z):.3f} 單位 ≈ {np.median(z)*S:.2f} m｜"
          f"光軸與 R^T·z 夾角 {ang:.2f}° {'✅' if ok else '❌'}")
if bad:
    raise SystemExit("❌ 符號自查未過 ⇒ 不匯出，先修座標約定")

Cen = np.array([c["center"] for c in cams]).mean(axis=0)
out = {"meta": {"mm_per_unit": round(S * 1000, 1), "fx": FX, "fy": FY, "cx": CX, "cy": CY,
                "w": W, "h": H, "registered": len(cams), "points": len(P3),
                "mean_vis": int(np.mean([len(c["vis"]) for c in cams])),
                "measured_travel_mm": 336, "span_units": 9.957, "mre_px": 0.865},
       "pts": np.round((pts - Cen) * S, 4).tolist(),
       "rgb": rgb,
       "cams": [{"name": c["name"],
                 "center": [round(v, 4) for v in ((c["center"] - Cen) * S)],
                 "view_dir": [round(v, 4) for v in c["view_dir"]],
                 "quat_wxyz": [round(v, 6) for v in c["quat_wxyz"]],
                 "vis": c["vis"]} for c in cams]}
(D / "viewer_export.json").write_text(json.dumps(out, ensure_ascii=False))
print(f"\n  → viewer_export.json {os.path.getsize(D/'viewer_export.json')/1024:.0f} KB"
      f"｜每張平均可見點 {out['meta']['mean_vis']}")

from PIL import Image
TH.mkdir(exist_ok=True)
n = 0
for c in cams:
    f = D / "images" / c["name"]
    if f.exists():
        im2 = Image.open(f).convert("RGB")
        im2.thumbnail((384, 288))
        im2.save(TH / (pathlib.Path(c["name"]).stem + ".jpg"), quality=8)
        n += 1
print(f"  → thumbs/ {n} 張（PINHOLE 畸變全 0 ⇒ 不需去畸變）")
