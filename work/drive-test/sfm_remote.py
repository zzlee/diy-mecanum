#!/usr/bin/env python3
"""Stage 1：在 GPU server 上用 pycolmap 跑增量式 SfM，判定這段行車影片能不能重建。
   同時匯出相機軌跡，回答「前進時車有沒有跑歪/偏航」。"""
import json
import pathlib
import shutil
import sys
import time

import pycolmap

import os
D = pathlib.Path(os.environ.get("SFM_DATA", "/data/suchen/3dscan/data/runA"))
IMGS = D / "images"
DB = D / "database.db"
OUT = D / "sparse"


def try_call(label, fn, *kwsets):
    """依序試不同的 kwargs 組合，避開 pybind11 簽名差異"""
    last = None
    for kwargs in kwsets:
        try:
            print(f"  {label}: kwargs={list(kwargs) or '無'}")
            return fn(**kwargs)
        except TypeError as e:
            last = e
            print(f"    ↻ 不接受: {str(e)[:120]}")
    raise SystemExit(f"❌ {label} 所有簽名都失敗：{last}")


def main():
    t0 = time.time()
    resume = "--resume" in sys.argv
    if DB.exists() and not resume:
        DB.unlink()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    names = sorted(p.name for p in IMGS.glob("*.jpg"))
    print(f"影格 {len(names)} 張｜pycolmap {pycolmap.__version__}｜build {getattr(pycolmap,'COLMAP_build','?')}")

    dev_cpu = getattr(pycolmap.Device, "cpu", None) or getattr(pycolmap.Device, "auto")
    ex = pycolmap.FeatureExtractionOptions()
    for attr, val in (("max_image_size", 1024), ("num_threads", -1)):
        try:
            setattr(ex, attr, val)
        except AttributeError:
            pass
    # SIFT GPU 需要 OpenGL/CUDA 版 wheel；這裡寧可固定 CPU（70 張 640×480 很快）
    sift = getattr(ex, "sift", None)
    if sift is not None:
        for attr in ("use_gpu",):
            try:
                setattr(sift, attr, False); print(f"  SiftExtraction.{attr}=False")
            except AttributeError:
                pass

    if not resume:
        try_call("extract_features", pycolmap.extract_features,
                 dict(database_path=str(DB), image_path=str(IMGS), camera_mode=pycolmap.CameraMode.SINGLE,
                      extraction_options=ex, device=dev_cpu),
                 dict(database_path=str(DB), image_path=str(IMGS), camera_mode=pycolmap.CameraMode.SINGLE,
                      extraction_options=ex),
                 dict(database_path=str(DB), image_path=str(IMGS), camera_mode=pycolmap.CameraMode.SINGLE))

        print("\n=== 2) 匹配（exhaustive + 幾何驗證）===" if False else "\n=== 2) 匹配（exhaustive + 幾何驗證）===")
        mo = pycolmap.FeatureMatchingOptions()
        sifm = getattr(mo, "sift", None)
        if sifm is not None:
            for attr in ("use_gpu", "guided_matching"):
                try:
                    setattr(sifm, attr, False if attr == "use_gpu" else True)
                except AttributeError:
                    pass
        fn = getattr(pycolmap, "match_exhaustive", None) or getattr(pycolmap, "match_image_pairs", None)
        try_call("match", fn,
                 dict(database_path=str(DB), matching_options=mo, two_view_geometric_verification=True),
                 dict(database_path=str(DB), match_options=mo, two_view_geometric_verification=True),
                 dict(database_path=str(DB), two_view_geometric_verification=True),
                 dict(database_path=str(DB)))

    try:
        db = pycolmap.Database.create(str(DB))
        print(f"  幾何驗證後的 inlier 配對組數: {db.num_inliers()}")
    except Exception as e:
        print(f"  （inlier 統計略過：{type(e).__name__}）")

    print("\n=== 3) 增量式建圖 ===")
    opts = pycolmap.IncrementalPipelineOptions()
    for attr, val in (("num_threads", -1), ("ba_use_gpu", False), ("mapper_use_GPU", False)):
        try:
            setattr(opts, attr, val)
        except AttributeError:
            pass
    recons = pycolmap.incremental_mapping(str(DB), str(IMGS), str(OUT), options=opts)
    print(f"  產生 {len(recons)} 個重建結果｜耗时 {time.time()-t0:.1f} s")

    summary = []
    for rid, rec in sorted(recons.items()):
        try:
            reg = list(rec.reg_image_ids())
        except AttributeError:
            reg = [im.image_id for im in rec.images() if im.is_registered()] if hasattr(rec, "images") else []
        n3 = rec.num_points3D() if hasattr(rec, "num_points3D") else len(list(rec.points3D()))
        try:
            mre = rec.compute_mean_reprojection_error()
        except Exception:
            mre = float("nan")
        print(f"\n  --- 重建 #{rid}: 註冊 {len(reg)}/{len(names)} 張｜3D 點 {n3}｜平均重投影誤差 {mre:.3f} px")
        # 軌跡：相機光心
        centers = []
        for iid in sorted(reg):
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
                c = im.projection_center()
            if c is not None:
                centers.append({"image_id": int(iid), "name": im.name, "center": [float(x) for x in c]})
        if centers:
            p0, p1 = centers[0]["center"], centers[-1]["center"]
            d = sum((a - b) ** 2 for a, b in zip(p0, p1)) ** 0.5
            print(f"      軌跡 {len(centers)} 個光心：首 {tuple(round(x,3) for x in p0)} → 末 {tuple(round(x,3) for x in p1)}")
            print(f"      首尾直線距離 {d:.3f}（COLMAP 座標單位＝尺度未定，需靠磁磚間距錨定）")
            # 朝向變化＝偏航角
            import math

            def yaw_of(im):
                cf = getattr(rec.image(im), "cam_from_world", None)
                if cf is None:
                    return None
                try:
                    q = cf.rotation  # Eigen Quaternion
                    # 光軸 = R * [0,0,1]
                    x, y, z, w = q.w, q.x, q.y, q.z if hasattr(q, "w") else (q[3], q[0], q[1], q[2])
                    # 旋轉矩第三列（R 的行向量寫法）
                    r02 = 2 * (x * z + w * y)
                    r12 = 2 * (y * z - w * x)
                    r22 = 1 - 2 * (x * x + y * y)
                    return math.degrees(math.atan2(r02, r22)), math.degrees(math.atan2(-r12, math.hypot(r02, r22)))
                except Exception:
                    return None
            ya = [yaw_of(c["image_id"]) for c in centers]
            ya = [v for v in ya if v]
            if len(ya) >= 2:
                print(f"      光軸朝向：首 {ya[0][0]:+.1f}°/俯 {ya[0][1]:+.1f}° → 末 {ya[-1][0]:+.1f}°/俯 {ya[-1][1]:+.1f}°"
                      f"  ⇒ 偏航變化 {ya[-1][0]-ya[0][0]:+.1f}°、俯仰變化 {ya[-1][1]-ya[0][1]:+.1f}°")
        summary.append({"recon": rid, "registered": len(reg), "images_total": len(names),
                        "points3D": int(n3), "mean_reproj_px": float(mre), "centers": centers})

    (D / "sfm_result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    best = max(summary, key=lambda s: s["registered"]) if summary else None
    print("\n=== 判定 ===")
    if not best or best["registered"] < 5:
        print("  🔴 註冊張數過低 ⇒ 這段影片無法重建（相機+開環慢走的路線到此為止，改 RGB-D）")
    elif best["mean_reproj_px"] > 1.5:
        print(f"  🟡 註冊 {best['registered']} 張但重投影誤差 {best['mean_reproj_px']:.2f} px 偏大 ⇒ 可重建但品質差，需改善拍攝")
    else:
        print(f"  ✅ 註冊 {best['registered']}/{len(names)} 張、{best['points3D']} 點、重投影 {best['mean_reproj_px']:.2f} px ⇒ pipeline 可用，值得優化拍攝")
    print(f"  明細 → {D}/sfm_result.json｜耗時 {time.time()-t0:.1f} s")


if __name__ == "__main__":
    main()
