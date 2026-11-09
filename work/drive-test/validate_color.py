import numpy as np, pycolmap, pathlib
from PIL import Image
M = dict(fx=473.451, fy=474.217, cx=323.551, cy=238.602, W=640, H=480)
D = pathlib.Path("/data/suchen/3dscan/data/runA")
rec = pycolmap.Reconstruction(str(D/"sparse"/"0"))
P3 = {k: p for k, p in rec.points3D.items()}
rng = np.random.default_rng(1234)
imgs = [im for im in (rec.images.values() if hasattr(rec.images,"values") else rec.images())]
REG = set(int(i) for i in rec.reg_image_ids())
d1s, d2s, frac = [], [], []
for im in imgs:
    if int(im.image_id) not in REG: continue
    f = D/"images"/im.name
    if not f.exists(): continue
    img = np.asarray(Image.open(f).convert("RGB"), dtype=np.float32)
    cf = im.cam_from_world(); R = np.asarray(cf.rotation.matrix(), float); C = np.asarray(im.projection_center(), float)
    p2 = im.points2D.values() if hasattr(im.points2D,"values") else list(im.points2D)
    XYZ = np.asarray([P3[o.point3D_id].xyz for o in p2 if o.point3D_id in P3], dtype=float)
    COL = np.asarray([P3[o.point3D_id].color for o in p2 if o.point3D_id in P3], dtype=float)
    if len(XYZ) < 20: continue
    pc = (XYZ - C) @ R.T
    z = pc[:,2]
    ok = z > 0.05
    u = np.round(M["fx"]*pc[ok,0]/z[ok] + M["cx"]).astype(int)
    v = np.round(M["fy"]*pc[ok,1]/z[ok] + M["cy"]).astype(int)
    ins = (u>=0)&(u<M["W"])&(v>=0)&(v<M["H"])
    u, v, c3 = u[ins], v[ins], COL[ok][ins]
    if len(u) < 20: continue
    d1 = np.abs(img[v, u] - c3).mean(axis=1)                        # 投影位置
    ru = rng.integers(0, M["W"], len(u)); rv = rng.integers(0, M["H"], len(u))
    d2 = np.abs(img[rv, ru] - c3).mean(axis=1)                      # 隨機對照
    d1s.append(d1.mean()); d2s.append(d2.mean()); frac.append((d1 < d2).mean())
d1, d2, fr = np.mean(d1s), np.mean(d2s), np.mean(frac)
print(f"影格數 {len(d1s)}")
print(f"  投影位置顏色差   = {d1:.2f} / 255")
print(f"  隨機位置顏色差   = {d2:.2f} / 255（對照組）")
print(f"  ⇒ 對齊使誤差降低 {(1-d1/d2)*100:.1f}%；逐點有 {fr*100:.1f}% 比隨機更貼近實物")
print(f"  判定：{'✅ 位姿與內參在像素層面對得上（重建不是僥倖）' if (1-d1/d2)>0.15 and fr>0.6 else '🟡 對齊優於隨機但幅度有限（紋理不足）' if (1-d1/d2)>0.05 else '🔴 對齊與隨機無異 ⇒ 位姿不可信'}")
