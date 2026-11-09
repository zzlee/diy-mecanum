#!/usr/bin/env python3
"""固定內參版 SfM：把標定好的 PINHOLE 內參寫進相機模型並凍結，讓 BA 只解姿態。
   用法：SFM_DATA=/data/suchen/3dscan/data/runB python -u sfm_fixed.py"""
import json
import os
import pathlib
import shutil
import sys
import time

import numpy as np
import pycolmap

# 本機相機標定（640x480，畸變係數全 0 → PINHOLE）
FX, FY, CX, CY = 473.451, 474.217, 323.551, 238.602

D = pathlib.Path(os.environ.get("SFM_DATA", "/data/suchen/3dscan/data/runA"))
IMGS, DB, OUT = D / "images", D / "database.db", D / "sparse"


def span(centers):
    """軌跡的最大兩兩距離＝有效基線跨度"""
    if len(centers) < 2:
        return 0.0
    P = np.asarray([c["center"] for c in centers])
    return float(np.max(np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)))


def run(tag):
    t0 = time.time()
    if DB.exists():
        DB.unlink()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    names = sorted(p.name for p in IMGS.glob("*.jpg"))

    ro = pycolmap.ImageReaderOptions()
    ro.camera_model = "PINHOLE"
    ro.camera_params = f"{FX},{FY},{CX},{CY}"
    ex = pycolmap.FeatureExtractionOptions()
    try:
        ex.max_image_size = 1024
    except AttributeError:
        pass
    dev = getattr(pycolmap.Device, "cpu", None) or getattr(pycolmap.Device, "auto")
    pycolmap.extract_features(str(DB), str(IMGS), camera_mode=pycolmap.CameraMode.SINGLE,
                              reader_options=ro, extraction_options=ex, device=dev)
    pycolmap.match_exhaustive(str(DB))   # 此版 API 只吃 database_path（內建幾何驗證）
    try:
        db = pycolmap.Database.open(str(DB))
        extra = f"｜inlier 配對 {db.num_inlier_matches()}｜相機模型 {db.read_all_cameras()[0].model.name}"
        db.close()
    except Exception as e:
        extra = f"｜（內參統計略過 {type(e).__name__}）"
    print(f"[{tag}] 影格 {len(names)}{extra}")

    opts = pycolmap.IncrementalPipelineOptions()
    for attr in ("ba_refine_focal_length", "ba_refine_principal_point", "ba_refine_extra_params"):
        try:
            setattr(opts, attr, False)          # 凍結內參
        except AttributeError:
            print(f"  （欄位 {attr} 不存在）")
    try:
        opts.mapper.fix_intrinsics_immediately = True
    except Exception:
        pass
    try:
        opts.ba_use_gpu = False
    except AttributeError:
        pass

    recons = pycolmap.incremental_mapping(str(DB), str(IMGS), str(OUT), options=opts)
    rows = []
    for rid, rec in sorted(recons.items()):
        reg = sorted(rec.reg_image_ids())
        centers = []
        for iid in reg:
            im = rec.image(iid)
            c = None
            cf = getattr(im, "cam_from_world", None)
            if cf is not None:
                c = getattr(cf, "translation", None)
                if c is None and hasattr(cf, "inverse"):
                    try:
                        c = cf.inverse().translation
                    except Exception:
                        c = None
            if c is None and hasattr(im, "projection_center"):
                try:
                    c = im.projection_center()
                except Exception:
                    c = None
            if c is not None:
                centers.append({"name": im.name, "center": [float(x) for x in c]})
        pts = rec.num_points3D()
        mre = rec.compute_mean_reprojection_error()
        s = span(centers)
        # 場景深度範圍（點雲到第一台相機）
        dz = None
        try:
            P = np.asarray([p.xyz for p in rec.points3D.values()] if hasattr(rec.points3D, "values")
                           else [p.xyz for p in rec.points3D()])
            c0 = np.asarray(centers[0]["center"])
            d = np.linalg.norm(P - c0, axis=1)
            dz = (float(np.percentile(d, 5)), float(np.percentile(d, 95)))
        except Exception:
            pass
        if len(centers) != len(reg):
            print(f"    ⚠️ 光心只取到 {len(centers)}/{len(reg)}，跨度數字不可信")
        # 匯出 PLY 點雲
        try:
            P3 = np.asarray([q.xyz for q in (rec.points3D.values() if hasattr(rec.points3D, "values") else rec.points3D())])
            with open(D / f"points_{rid}.ply", "w") as fh:
                fh.write("ply\nformat ascii 1.0\nelement vertex %d\nproperty float x\nproperty float y\nproperty float z\nend_header\n" % len(P3))
                for v in P3:
                    fh.write(f"{v[0]:.4f} {v[1]:.4f} {v[2]:.4f}\n")
            print(f"    點雲 → points_{rid}.ply（{len(P3)} 點）")
        except Exception as e:
            print(f"    （PLY 匯出失敗 {type(e).__name__}）")
        print(f"  #{rid}: 註冊 {len(reg)}/{len(names)}｜3D 點 {pts}｜重投影 {mre:.3f} px｜"
              f"基線跨度 {s:.3f} 單位" + (f"｜點雲深度 p5–p95 {dz[0]:.2f}–{dz[1]:.2f}" if dz else ""))
        rows.append({"tag": tag, "recon": rid, "registered": len(reg), "images": len(names),
                     "points3D": int(pts), "mean_reproj_px": float(mre), "baseline_span": s,
                     "depth_p5_p95": dz, "centers": centers})
    (D / "sfm_fixed.json").write_text(json.dumps(rows, ensure_ascii=False))
    print(f"[{tag}] 完成，耗時 {time.time()-t0:.1f} s")
    return rows


BASE = pathlib.Path("/data/suchen/3dscan/data")
for tag in (sys.argv[1:] or ["runA", "runB"]):
    path = str(BASE / tag)
    if pathlib.Path(path, "images").exists():
        os.environ["SFM_DATA"] = path
        globals()["D"] = pathlib.Path(path)
        globals()["IMGS"], globals()["DB"], globals()["OUT"] = D / "images", D / "database.db", D / "sparse"
        try:
            run(tag)
        except Exception as e:
            print(f"[{tag}] ❌ {type(e).__name__}: {str(e)[:200]}")
    print()
