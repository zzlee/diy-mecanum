]777;notify;Ready for input;�明：這個 session 我只拿到 web_search / fetch_content / read 三個工具， 沒有寫檔或開 shell 的工具，所以無法建立 work/3dgs-research/REPORT.md。以下是完整 報告內容（可直接存成該路徑）。 3DGS on 無 root Ubuntu 22.04 + RTX PRO 6000 Blackwell (sm_120)…無法寫檔說明：這個 session 我只拿到 `web_search` / `fetch_content` / `read` 三個工具，**沒有寫檔或開 shell 的工具**，所以無法建立 `work/3dgs-research/REPORT.md`。以下是完整報告內容（可直接存成該路徑）。

---

# 3DGS on 無 root Ubuntu 22.04 + RTX PRO 6000 Blackwell (sm_120) 可行性調研

調研基準：以 2026 年 10 月底之後的網路狀態為準（PyPI 上 torch 最新穩定版為 **2.15.0，2026-10-28**；CUDA 文件已到 13.5；gsplat 最新 tag 為 v1.5.3）。本機未做任何安裝/編譯。

## 【結論】有條件可行

- **可行，但「一定要自己編一次 CUDA extension」**。gsplat 官方預編譯 wheel **不支援 sm_120**（官方 wheel index 只到 `pt24cu124`，且 CI 的 `TORCH_CUDA_ARCH_LIST` 根本沒列 12.0）；`pip install gsplat` 拿到的 PyPI 輪子是純 Python + **首次 import 時 JIT 編譯**，所以**仍然需要 nvcc + g++**。
- 免 root 拿到 nvcc 有**三條成熟路**：conda-forge/`cuda-toolkit`（最穩）、PyPI `cuda-toolkit[nvcc]` 元包（可行但佈局需自己拼）、CUDA runfile `--extract`／`--toolkitpath`（官方文件允許裝到可寫目錄）。
- 完全免編譯的備援存在（BRUSH 走 WebGPU；純 PyTorch 3DGS 實作若干），但品質/時間成本明顯較高。
- 瀏覽器端**完全是已解決問題**：SuperSplat viewer / mkkellogg / antimatter15 都是 MIT、可離線自架。

### 推薦第一條安裝路徑（可直接貼上；`< >` 之類需自行確認）

```bash
# 0) 前提檢查（全免 root）
nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv   # 期望 12.0
gcc --version || echo "NO HOST G++ → 必須用 conda-forge gxx_linux-64"

# 1) 免 root 的 CUDA 工具鏈，裝進 /data（conda-forge 有 12.8.x / 13.x）
conda create -y -p /data/envs/gs python=3.11 && conda activate /data/envs/gs
conda install -y -c conda-forge cuda-toolkit=13.0 gxx_linux-64=12

# 2) torch：選「與 nvcc 同大版本」且含 sm_120 的 wheel（cu126 的 torch build 沒有 sm_120）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
python - <<'PY'
import torch
from torch.utils.cpp_extension import CUDA_HOME
print(torch.__version__, torch.version.cuda, CUDA_HOME)
print("arch_list:", torch.cuda.get_arch_list())        # 必須看到 sm_120（或 compute_120）
print("capability:", torch.cuda.get_device_capability())# (12, 0)
PY

# 3) 環境變數（建議寫進 $CONDA_PREFIX/etc/conda/activate.d/env_vars.sh）
export CUDA_HOME=$CONDA_PREFIX
export PATH=$CUDA_HOME/bin:$PATH
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:$LD_LIBRARY_PATH
export TORCH_CUDA_ARCH_LIST="12.0+PTX"     # 12.0+PTX = 原生 cubin + 向前相容 PTX
export MAX_JOBS=16
nvcc --version                              # 必須 >= 12.8（CUDA 12.6 不認 sm_120）

# 4) gsplat：來源建置，務必 --no-build-isolation（否則建置環境看不到 torch）
pip install ninja numpy jaxtyping rich einops tyro opencv-python-headless pycolmap
pip install --no-build-isolation --no-deps "git+https://github.com/nerfstudio-project/gsplat.git@<pin 一個 commit>"
python -c "import gsplat, torch; print('gsplat ok', gsplat.__version__)"

# 5) 訓練（640x480 / 70-200 張 用「小場景」參數；旗標名稱請先 --help 核對）
git clone --recursive https://github.com/nerfstudio-project/gsplat.git && cd gsplat
python examples/simple_trainer.py default \
  --data_dir /data/scene --result_dir /data/out/scene \
  --data_factor 1 --sh_degree 3 \
  --max_steps 7000 --save_steps 7000 --ply_steps 7000 \
  --opacity_reg 0.01 --scale_reg 0.01 --random_bkgd \
  --strategy.refine_stop_iter 6000 --viewer.port 8080
# 即時預覽：ssh -L 8080:localhost:8080 <host>  然後開 http://localhost:8080

# 6) 輸出 → 瀏覽器靜態預覽（需 Node.js）
npx --yes @playcanvas/splat-transform /data/out/scene/ply/point_cloud_7000.ply /data/web/scene.sog
git clone https://github.com/playcanvas/supersplat-viewer && cd supersplat-viewer && npm i && npm run build
python3 -m http.server 8000 -d dist      # 開 /?content=/data/web/scene.sog（視 dist 佈局）
```

---

## 【各題回答】

### 1. gsplat 是否支援 sm_120？有沒有免編譯的預編譯 wheel？

**版本現況**
- 最新 release/tag：**v1.5.3**（PyPI 上傳 2025-07-04；GitHub tags 最新即 v1.5.3，**無 v1.6.0 tag**）→ https://github.com/nerfstudio-project/gsplat/tags 、https://pypi.org/pypi/gsplat/json
- `main` 是 v1.6.0-dev，README 明載：**「PyTorch 2.7+ is now required, with improved CUDA 12.8 and 13.2 build compatibility」**、**「CUDA 13 and NumPy 2 support」** → https://github.com/nerfstudio-project/gsplat/blob/main/README.md
- `pip install gsplat` 裝到的是 `gsplat-1.5.3-py3-none-any.whl`（**無二進位 CUDA**），README 說這樣會在**第一次執行時 JIT 編譯 CUDA code** → 一樣需要 nvcc。

**sm_120 支援：source build 可行，prebuilt wheel 不可行（決定性證據）**
- 官方 wheel index `https://docs.gsplat.studio/whl/gsplat/` 只有 **pt20–pt24 × cu118/cu121/cu124，且只有 cp310**；實測 `.../whl/pt27cu128/`、`.../whl/pt28cu128/`、`.../whl/pt210cu130/` 全部 **HTTP 404**。
- 更關鍵：CI 的建置腳本 `main/.github/workflows/cuda/Linux-env.sh` 對 **cu128/cu129/cu130 都只設 `TORCH_CUDA_ARCH_LIST="7.5;8.0;8.6;9.0;9.0a"`**（Windows 分支 `"7.5;8.0;8.6;9.0"`）→ 即使将来釋出 cu128/cu130 輪子，**裡面的 cubin 也不含 sm_120、也沒有 PTX**，跑起來就是 `CUDA error: no kernel image is available for execution on the device`。 → https://raw.githubusercontent.com/nerfstudio-project/gsplat/main/.github/workflows/cuda/Linux-env.sh
- 使用者回報與維護者脈絡一致：issue **#849**「官方 wheel CUDA ≤12.4 不含 SM_120，`Building from source works for us on both Windows and Linux`」；issue **#855**（CUDA 12.6 不認 sm_120，nvcc 12.6 編不過）；issue **#1053**（RTX 5090 + torch cu126 觸發 same error，解法是換成 **cu128 的 torch wheel**）。
  https://github.com/nerfstudio-project/gsplat/issues/849 、 /issues/855 、 /issues/1053
- 來源建置在 Blackwell 上的正向證據：PR **#1085**（作者自測 **RTX 5090 sm_120 + CUDA 13.0/13.3 + torch cu130 完整 `bdist_wheel` + fwd/bwd smoke test pass**，但環境是 Windows/MSVC）。https://github.com/nerfstudio-project/gsplat/pull/1085
- PyTorch 端：**PyTorch 2.7 起 cu128 wheel 支援 Blackwell**（官方 release 文 + ptrblck 於論壇：「PyTorch 2.7.0 already added Blackwell support on our PyTorch wheels built with CUDA 12.8」）→ https://pytorch.org/blog/pytorch-2-7/ 、https://discuss.pytorch.org/t/when-will-sm120-support-be-available/223621/2 ；2.7 的 cu128 build 編譯架構包含 `...;12.0+PTX`（issue #1053 留言引用 PyTorch `build_cuda.sh`）。
- **注意坑**：近期 torch（2.13/2.14/2.15）官方 index 只剩 **cu126 / cu130 / cu132**（無 cu128）→ https://pytorch.org/get-started/previous-versions/ 。其中 **cu126 的 build 不含 sm_120**（CUDA 12.6 無法目標 sm_120），所以務必 cu130/cu132，或改用旧的 `2.7.x/2.9.x + cu128`。
- 你的驅動 **580.82.09 = R580**：CUDA 12.8 需 ≥570.26；CUDA 13.x 需 **≥580**（minor-version compatibility）→ 兩者都可用。https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html
- 社群 wheel（**未驗證是否含 sm_120**）：fork `martinResearch/gsplat` release tag `1.5.3`（2026-03-06）提供 `gsplat-1.5.3+pt210cu128-cp310..cp313-linux_x86_64.whl`；另 `Enndee` 在 issue #1053 提供 `pt212cu130` win wheel「exclusively built for RTX 5090」。→ https://api.github.com/repos/martinResearch/gsplat/releases/tags/1.5.3

> 補充（與你資料直接相關）：gsplat `main` 已整合 **3DGUT（支援含畸差相機與 rolling shutter）** 與 **PPISP（影像品質後製/曝光白平衡補償）**，對「巡控車影片」場景有實質價值。

### 2. 一定要編譯時，無 root 怎麼做？

**(a) 只用 pip 的 nvidia-cuda-* 能否 build？— 可以，但要湊齊且版本必須一致；官方定位上是「runtime 用」**
- NVIDIA 官方文件 §8「Pip Wheels」：*"These packages are intended for **runtime use** and do not currently include developer tools (these can be installed separately)"* → https://docs.nvidia.com/cuda/cuda-installation-guide-linux/index.html
- NVIDIA 確實有發 **PyPI 元包 `cuda-toolkit`**（版本 11.8 → **13.5.0**）：https://pypi.org/simple/cuda-toolkit/ ；實務用法 `pip install "cuda-toolkit[nvcc]==13.3.0"`（vLLM 建置紀要 https://hiraditya.github.io/posts/building-vllm-from-source/ ；CUDA 12 時代對應 `nvidia-cuda-nvcc-cu12`（最新 **12.9.86**，可編 sm_120）https://pypi.org/project/nvidia-cuda-nvcc-cu12/ ）。
- **CUDA 13 的 pip 佈局是合併式 prefix**，可直接當 CUDA_HOME；公開 recipe（vLLM 紀要，逐步）：
  ```bash
  export CUDA_HOME=$VIRTUAL_ENV/lib/python3.*/site-packages/nvidia/cu13
  ln -sfn $CUDA_HOME/lib $CUDA_HOME/lib64
  ( cd $CUDA_HOME/lib && for f in lib*.so.*; do ln -sf "$f" "${f%%.so.*}.so"; done )
  mkdir -p $CUDA_HOME/lib/stubs && ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so $CUDA_HOME/lib/stubs/libcuda.so
  ```
- 已有專案把這條路做成正式支援：tilelang PR **#1833**「build tilelang without host toolchain … user does not have permission to install cuda toolkit」、PR **#1528**（從 `nvidia-cuda-nvcc` 反查 CUDA_HOME）、Lightricks `LTX-2/packages/ltx-kernels/setup.py::_prefer_pip_cuda_home()`。
- 已知失敗模式（必須避免）：只裝部分套件 → 缺 `cuda_runtime.h` / CCCL headers（MLX issue #3859）；**nvcc 與 cccl headers 次版本不一致** → `cuda_toolkit.h` 直接判定 compiler 不相容（odysseus issue #5608）。CCCL 就是 `nvidia-cuda-cccl-cu12/-cu13`，必須同次版本。
- 另有反例意見：部落格「A PyTorch + CUDA environment that can actually compile extensions」（2026-07）：*"The pip wheel brings enough CUDA runtime libraries for PyTorch to execute kernels. It does not give source builds a normal toolkit layout with a usable `nvcc` and one clear `CUDA_HOME`"* → 他改用 **pixi/conda-forge**（`cuda-version=13.0.*`、`cuda=*`、`gxx_linux-64=*`、`CUDA_HOME=$CONDA_PREFIX`、`TORCH_CUDA_ARCH_LIST="12.0+PTX"`，再 `pip install --no-build-isolation --no-deps git+gsplat`）。https://handysome6.github.io/blog/pytorch-cuda-env/
- **pip 沒有 g++**：若 host 沒有 `gcc/g++`（或版本太新/太舊），免 root 只有 conda-forge `gcc_linux-64/gxx_linux-64` 一途（PyTorch 論壇多次確認需要它）。
- gsplat 的建置腳本本身用 `torch.utils.cpp_extension.CUDA_HOME`，並會自動補 `CUDA_HOME/targets/*/include` 與 `.../include/cccl`（CCCL 3.0 headers 搬家問題已在 code 內處理）→ https://github.com/nerfstudio-project/gsplat/blob/main/gsplat/cuda/build.py

**結論 (a)：能，但建議把 pip-nvcc 當作「conda 不可用」時的備援；若用 pip 路，`cuda-toolkit` 元包 + 同次版本 + 手動 CUDA_HOME 拼裝 + conda-forge gxx 是必要組合。**

**(b) runfile `--toolkit --toolkitpath=/data/xxx --silent` 需要 root 嗎？— 官方文件：只有兩件事需要 root**
> "The installer must be executed with sufficient privileges to perform some actions. When the current privileges are insufficient … the installer will ask for the user's password"；需要 root 的動作是 ①*「installing the CUDA Toolkit to a location the user does not have permission to write to」* ②*「creating the `/usr/local/cuda` symbolic link」*
→ https://docs.nvidia.com/cuda/cuda-installation-guide-linux/index.html

文件記錄的選項包含：`--toolkit`、`--toolkitpath=<path>`、`--defaultroot=<path>`、`--extract=<path>`（*"Extracts the raw files of the toolkit to \<path\>"*）、`--override`、`--no-man-page`、`--tmpdir=<path>`、`--silent`（FAQ 例）。
- **最穩的免 root 用法**：`./cuda_xxx_linux.run --extract=/data/cuda-12.8.1`，完全不跑安裝器，再把 `CUDA_HOME` 指向解壓結果（需自行 `chmod +x`、必要時補 symlink）。
- `--toolkitpath=/data/... --silent --override --no-man-page --tmpdir=/data/tmp` 理论上不觸發 root 需求，但 **是否仍會因 `/usr/local/cuda` symlink 提示要密碼 → 未实測（未证实）**；`--extract` 是零風險替代。
- runfile 約 3–4 GB，`/data` 2.1TB 沒問題。

**(c) 需要設定的環境變數**
| 變數 | 值 | 出處 |
|---|---|---|
| `CUDA_HOME` | conda 環境 `$CONDA_PREFIX`；pip 版則 `…/site-packages/nvidia/cu13`；runfile 則 `/data/cuda-12.8.1` | gsplat CI `Linux-env.sh`、vLLM 紀要、`torch/utils/cpp_extension.py::_find_cuda_home()` |
| `PATH` | `$CUDA_HOME/bin:$PATH` | 同上（torch 找不到 nvcc 的最常見原因就是這個） |
| `LD_LIBRARY_PATH` | `$CUDA_HOME/lib64:$LD_LIBRARY_PATH`（連結期 `-lcudart`） | gsplat `Linux-env.sh` |
| `TORCH_CUDA_ARCH_LIST` | **`12.0+PTX`**（只写 `12.0` 也可，但沒 PTX 就無向前相容） | HF skills 文件（`TORCH_CUDA_ARCH_LIST="12.0+PTX"` for ZeroGPU RTX PRO 6000 sm_120）、handysome6 部落格 |
| `CC/CXX/CUDAHOSTCXX` | 指向 conda-forge g++（系統 gcc 不相容時） | PyTorch 論壇 threads |
| `MAX_JOBS` | 8–16（gsplat 對未設定的情況會用 10） | gsplat `build.py` / releases |
| pip 旗標 | **`--no-build-isolation`**（必需）＋ `--no-deps` | gsplat issue **#924**（`pip install --no-build-isolation .` 解掉 CUDA 12.8 建置失敗）、**#842**（isolation 下 `No module named 'torch'`） |

诊断命令：`python -c "from torch.utils.cpp_extension import CUDA_HOME; print(CUDA_HOME)"`、`nvcc --version`、`python -c "import torch;print(torch.cuda.get_arch_list())"`。

### 3. 「免自訂 CUDA kernel」的純 PyTorch / 免 nvcc 方案

| 方案 | 是否免 nvcc | 備註 / 來源 |
|---|---|---|
| **BRUSH**（ArthurBrussee/brush，Google Research 公開 fork） | ✅ 完全不碰 CUDA（Burn + WebGPU） | *"works on … AMD/Nvidia/Intel cards, Android, and in a browser"*、*"avoid large CUDA deps"*、*"Training is fully supported natively, on mobile, and in a browser"*；吃 **COLMAP 或 nerfstudio 格式**資料；可當 viewer（Chrome/Edge 134+）。https://github.com/ArthurBrussee/brush |
| **Mxbonn/gsplat-pytorch** | ✅ 純 PyTorch | *"does not use CUDA and is therefore runnable on all PyTorch supported devices"*，但作者自己寫：CUDA 版 **should be used for any performance critical applications**，此實作 *"mainly intended for research and educational purposes"*。https://github.com/Mxbonn/gsplat-pytorch |
| **hbb1/torch-splatting** | ✅ 純 PyTorch | 完整 3DGS 訓練流程；因 Python loop 慢，tile 從 16×16 改成 64×64。https://github.com/hbb1/torch-splatting |
| **arnaudstiegler/torch-gaussian-splatting-rasterizer** | ✅ | 自我聲明 *"pretty suboptimal and not meant to be used for training/evaluation"* → 只能做驗證/除錯。https://github.com/arnaudstiegler/torch-gaussian-splatting-rasterizer |
| **WebDGS**（瀏覽器 WebGPU 訓練） | ✅ | 靜態站內完成 forward/backward/densify；實驗性。https://github.com/krispy-kenay/WebDGS |
| **taichi-splatting**（Taichi 自带編譯器，不需 nvcc） | 大致 ✅ | 作者註明 active development、可能隨時壞；sm_120 支援**未证实**。https://github.com/urbste/taichi-splatting |
| **msplat**（Jittor） | ❌ 不是 | Jittor 執行期仍需 nvcc/g++（未证实 Blackwell 可用性）。https://github.com/pointrix-project/msplat |

**品質／時間代價**：演算法上限相同（同一套 3DGS），但沒有 fused kernel → 每步慢一個數量級級別、VRAM 用量更高；上述專案自己都不建議用於「performance critical」或正式評估，**具體倍數與 PSNR 差異無公開基準（未证实）**。優點是你的場景很小（640×480、≤200 張、96GB VRAM），慢也跑得動；缺點是 densification/SH 優化這些「工程细节」在純 PyTorch 版通常落後 gsplat（無 MCMC、無 absgrad、無 antialiased、無 3DGUT）。

### 4. 瀏覽器即時預覽：格式、離線自架、授權

| 工具 | 吃的格式 | 離線/自架 | 授權 |
|---|---|---|---|
| **mkkellogg/GaussianSplats3D**（Three.js） | README 列 `.ply`(INRIA) / `.splat` / `.ksplat`；source 內另有 **SPZ loader** 與 PlayCanvas compressed-PLY parser | ✅ `npm run build` / `@mkkellogg/gaussian-splats-3d`；README：*"Built-in viewer is self-contained so very little code is necessary to load and view a scene"*；內建 PLY→KSPLAT（`node util/create-ksplat.js [path] [output] [level]`，level 0/1/2 壓縮 SH 到 16/8-bit） | **MIT**（GitHub license API 確認） https://github.com/mkkellogg/GaussianSplats3D |
| **SuperSplat（PlayCanvas 編輯器）** | 匯入/匯出 .ply、.sog、glb 等（README 未逐條列格式 → 詳細清單**未证实**） | ✅ `npm install && npm run develop` → localhost:3000；線上版 superspl.at/editor（**家庭影像若在意隱私請自架**） | **MIT** https://github.com/playcanvas/supersplat |
| **supersplat-viewer** | `?content=` 支援 **`.ply`、`.compressed.ply`、`.sog`、`.meta.json`(unbundled SOG)、`.lod-meta.json`(streamed LOD)** | ✅ *"compiles to a simple, self-contained static website"*（`npm run build` 後丟任何靜態主機） | **MIT** https://github.com/playcanvas/supersplat-viewer |
| **splat-transform**（CLI 轉換） | 讀：.ply/.compressed.ply/.sog/.spz/.splat/.ksplat/.lcc；**寫**：.ply/.compressed.ply/.sog/.spz（**不能寫 .splat/.ksplat**）；並可做 `--decimate`、`-V opacity,gt,0.5`、`--filter-nan`、位移/旋轉、合併 | ✅ Node.js CLI | **MIT** https://github.com/playcanvas/splat-transform |
| **antimatter15/splat** | `.splat`（最輕量、老手機也能跑） | ✅ 純靜態 | **MIT** https://github.com/antimatter15/splat |
| **gsplat 內建 viewer**（`examples/simple_trainer.py` → `viser` + `nerfview`，port 8080） | 直接顯示**訓練中**的 splats（不需檔案） | ⚠️ 需要 Python 行程常駐（遠端用 SSH tunnel）；不是靜態檔 viewer | gsplat **Apache-2.0**、viser **Apache-2.0**、nerfview **Apache-2.0**（GitHub license API 確認；PyPI `nerfview` 最新 **0.1.3**，*"built on viser"*） https://github.com/nerfstudio-project/gsplat/blob/main/examples/simple_trainer.py |
| （加分）Gradio `gr.Model3D` | 原生渲染 gaussian `.ply` / `.splat`（Babylon.js） | ✅ | Apache-2.0；適合「渲染結果 vs 實拍照片」並排頁面。https://github.com/huggingface/skills/blob/main/skills/huggingface-spaces/references/3d-gsplat.md |

實務提醒：INRIA `.ply`（SH degree 3）約 250 bytes/splat，10 萬–100 萬 splat 就是 25–250 MB，瀏覽器會卡；**上線請轉 `.sog` 或 `.compressed.ply`**（`splat-transform`）。⚠️ gsplat 附的 Python `ply2sogs` 在 Blackwell 上有回報壞掉（cupy/torchpq `max_dynamic_shared_size_bytes`）→ issue **#807**，所以用 Node 版 `splat-transform` 較穩。

### 5. 640×480、70–200 張、基線 0.3–1.0 m、視角集中的單目資料

**預期問題（有文獻明载）**
- *"this technique requires dense training views … A limited number of input views will significantly degrade reconstruction quality, resulting in artifacts such as **floaters** and **background collapse**"* — SparseGS https://arxiv.org/html/2312.00206
- 3DGS 從 SfM 點初始化，*"falls short in few-shot settings: … inadequate initialization, leading to oversmoothed outcomes and a tendency to **overfit on training views**"* — FSGS https://arxiv.org/html/2312.00451
- 稀疏視角的兩種失敗模式：**近鏡頭區 Gaussian 過密而過擬合、遠處覆蓋不足而欠擬合** — D2GS https://arxiv.org/html/2510.08566
- 深度正規化 / 單目深度先驗是主流解方 — DNGaussian https://arxiv.org/html/2403.06912 、CVPRW'24 https://arxiv.org/html/2311.13398
- 拍摄面：視角太集中 → 換個位置看就崩（parallax 不足）；建議「每個面至少 3 個視角、走位而非只轉頭」 https://rd.nytimes.com/projects/gaussian-splatting-guide/ 、https://radiancefields.com/splatking/guide/technique 、https://www.3dgsviewers.com/learn/guide/pipeline/capture ；整段錄影一次重建大空間效果差，**分區重建**較好 https://pmc.ncbi.nlm.nih.gov/articles/PMC11855968/

**建議參數（gsplat 預設值取自原始碼，務必用 `--help` 核對旗標名）**
| 項目 | 預設（main） | 你的場景建議 |
|---|---|---|
| `data_factor` | **4**（會把圖縮 4 倍） | **1**（640×480 本來就小，縮了會糊） |
| `sh_degree` / `sh_degree_interval` | 3 / 1000 | 先 3；出現色塊/虹彩感就降 2 或 1（也能縮小瀏覽器檔） |
| `max_steps` / `save_steps` / `ply_steps` | 30000 / [7000,30000] / [7000,30000] | **7000**（此解析度通常已收斂）；要衝品質再 30k |
| DefaultStrategy | `grow_grad2d 2e-4`, `refine_start_iter 500`, `refine_every 100`, `refine_stop_iter 15000`, `reset_every 3000`, `prune_scale3d 0.1`, `absgrad False` | 只跑 7000 步時把 `refine_stop_iter` 降到 **5000–6000**（否則密化窗口浪費）；薄結構/邊角多可試 `absgrad=True`（AbsGS arXiv:2404.10484） |
| MCMCStrategy（`simple_trainer.py mcmc`） | `cap_max 1_000_000`, `noise_lr 5e5`, `refine_start_iter 500`, `refine_stop_iter 25_000`, `refine_every 100`, `min_opacity 0.005`；子命令另帶 `init_opa 0.5, init_scale 0.1, opacity_reg 0.01, scale_reg 0.01` | **`cap_max` 降到 100k–300k**（小場景 1M 會長出大量 floaters）；MCMC 對「初始化差/視角少」較穩（arXiv:2404.09591） |
| `ssim_lambda` | 0.2 | 保持 0.2；紋理糊可試 0.1（提高 L1 比重） |
| `opacity_reg` / `scale_reg` | 0.0 / 0.0 | **0.01 / 0.01**（壓 floaters 最便宜的開關） |
| `random_bkgd` | False | **True**（隨機背景迫使半透明高斯變透明的常用技巧） |
| `near_plane` / `far_plane` | 0.01 / 1e10 | 依實際場景收 `far_plane`（例如 15–30 m）以抑制遠處飄浮物 |
| `pose_opt` / `pose_opt_lr` | False / 1e-5 | **考慮開 pose_opt**：車載影像的 COLMAP 位姿常有小誤差 |
| `app_opt` | False | 曝光/白平衡漂移明顯時開（或直接用 `main` 的 **PPISP**） |
| `antialiased` | False | 可試 True（縮圖/邊緣鋸齒情境）→ 增益**未证实** |
| 初始化 | `examples/datasets/colmap.py` 用 **`pycolmap.Reconstruction("…/sparse/0")`**，並用 `reconstruction.points3D` 做 normalize/初始化，`load_depths=True` 可把點雲投影成深度圖 | **必須保留 COLMAP 稀疏點雲**（`points3D`）；**密集 MVS 初始化非必要**（窄基線下 MVS 深度本身不可靠，且 gsplat 範例無內建密集初始化 → 未证实有增益）。若要做稀疏視角最佳化，走 monocular depth 先驗（DNGaussian/上述 CVPRW 文） |
| 資料切分 | 以固定間隔留 test frames | 影片相鄰幀高度重複，**先做近重複幀去重**，並用**隔段保留**（例如每 8 幀取 1 做 test）避免洩漏造成 PSNR 虚高 |

其他整合注意：`examples/requirements.txt`（main）pin **`torch==2.9.1` / `torchvision==0.24.1`、`pycolmap>=3.10.0`、`numpy>=2,<3`**，並依賴 `viser`、`nerfview@<commit>`、`fused-ssim`、`fused-bilagrid`、`ppisp`（都是 git 依賴）→ https://raw.githubusercontent.com/nerfstudio-project/gsplat/main/examples/requirements.txt 。**你用的是 pycolmap 4.2.1 → gsplat parser 對 4.x API 是否完全相容「未证实」**，請先跑：
`python -c "import pycolmap;r=pycolmap.Reconstruction('/data/scene/sparse/0');print(r.num_points3D(), len(r.cameras))"`

---

## 【風險與備援】

**主路徑失敗點排序**
1. `TORCH_CUDA_ARCH_LIST` 沒設對 → 還是 `no kernel image is available`（先跑 `torch.cuda.get_arch_list()` 分流：torch 本身沒有 sm_120 → 換 cu130/cu132 wheel；torch 有而 gsplat 沒有 → 重編 gsplat）。
2. 忘了 `--no-build-isolation` → `ModuleNotFoundError: No module named 'torch'` / CUDA 12.8 建置失敗（issue #842/#924）。
3. CUDA major 與 torch wheel 不一致（nvcc 12.x vs torch cu130）→ `cpp_extension._check_cuda_version` 报错；**nvcc 與 cccl 次版本不一致** → `cuda_toolkit.h` 判定不相容。請「同 major、尽量同 minor」。
4. host 無 g++ / g++ 太新超出該 CUDA 支援上限 → 需 conda-forge `gxx_linux-64` 並用 `CUDAHOSTCXX`/`--override`。
5. gsplat `main` 未 release、API 漂移（`examples/requirements.txt` pin 的 torch 2.9.1 與你裝的 2.15 不一致）→ 用 **pin commit** 而非 `main` HEAD。

**備援一（同為 gsplat，換「已知可用時代」組合）**
`torch 2.7.1/2.9.1 + cu128`（cu128 wheel 含 sm_120）+ **conda-forge `cuda-toolkit=12.8`** + **gsplat tag `v1.5.3`** 來源建置（CUDA 13/NumPy 2 相關修正尚未進入 1.5.3，因此 1.5.3 請走 CUDA 12.8）。這是 gsplat examples、HF 文件（facts as of 2026-07）與 issue #924 交錯驗證過的區間。

**備援二（不編 gsplat）**
- 用社群預編譯 **`diff_gaussian_rasterization`（INRIA upstream API）sm_120 wheel**：HF dataset `multimodalart/zerogpu-blackwell-wheels`，例 `wheels/pt211-cu130-cp312/diff_gaussian_rasterization-0.0.0-cp312-cp312-linux_x86_64.whl`（同目錄還有 flash_attn/xformers/pytorch3d/nvdiffrast）→ https://huggingface.co/datasets/multimodalart/zerogpu-blackwell-wheels/tree/main/wheels 。限制：**沒有 `simple_knn` 輪子**（原 INRIA 版 gaussian-splatting 需要它）→ 仍要自己編 `simple-knn`（很小）；且 ashawkey fork（4-tuple）無輪子。來源：https://github.com/huggingface/skills/blob/main/skills/huggingface-spaces/references/3d-gsplat.md 、`3d-cuda-extensions.md`
- 或直接用 **BRUSH（WebGPU，零 CUDA）** 訓練同一份 COLMAP 資料，並用它內建網頁 viewer 展示。

**備援三（完全免編譯／免 GPU 端建置）**
- 純 PyTorch 3DGS（`gsplat-pytorch` / `torch-splatting`）：能跑通流程、品質與時間成本較高。
- 把「建置」搬到別台機器：`TORCH_CUDA_ARCH_LIST="12.0+PTX" pip wheel .` 产出輪子後 copy 到本機安裝（HF 文件的 Strategy 1），可徹底繞過本機工具鏈。
- 若以上全滅：展示層仍可上線 —— 用現有 COLMAP 點雲做 point-splat 預覽 + 實拍照片並排，等訓練環境就緒再補 3DGS。

---

## 【未证实事項】

1. 社群 wheel（`martinResearch/gsplat` pt210cu128、Enndee 的 `pt212cu130`）**是否真的內含 sm_120 cubin** — 只能装完跑 kernel smoke test。
2. **CUDA runfile 純非 root 全流程**：官方只说「寫入不可寫目錄」與「建 `/usr/local/cuda` symlink」需要 root；`--toolkit --toolkitpath --silent` 是否完全不提示密碼 **未实測**（`--extract` 可繞過）。
3. **cu12 系列的 pip 佈局**能否無縫拼出 gsplat 需要的 CUDA_HOME（公開 recipe 多為 **cu13** 合併佈局；cu12 的 `nvidia/cuda_nvcc`、`cuda_runtime`、`cuda_cccl` 是分開目錄，需自己做 include/lib 合併）— **未实測**。
4. `pip install torch`（PyPI 預設，非 index-url）在 2.15.0 綁的是哪個 CUDA build、是否含 sm_120 — **未证实**，必須用 `torch.cuda.get_arch_list()` 现场确认。
5. **gsplat `main`（v1.6.0-dev）在 Linux + CUDA 13 + sm_120 的端到端建置**：目前只有 Windows/CUDA13/sm_120 的作者自測（PR #1085）與 issue #849 的口述「Linux 可從來源建置」。
6. **nerfstudio PR #3771（Blackwell/sm_120 支援）是否已合併** — 抓取到的欄位互相矛盾（state open + 有 merge commit sha）。
7. 純 PyTorch 3DGS 相對 gsplat 的**具體 slowdown 倍數 / PSNR 差距** — 無公開基準。
8. **BRUSH 是否輸出 .ply/.splat**、以及它與 gsplat 在此類室內場景的品質差距 — README 只确认可**載入** .ply/.compressed.ply 檢視。
9. gsplat `ply2sogs`（SOG 壓縮）在 sm_120 上是否已修復 — issue **#807** 仍未結。
10. `--antialiased`、`app_opt`、MCMC `cap_max` 對「窄基線、低解析、前視」場景的**具體增益幅度** — 無對應公開 benchmark。
11. **COLMAP/pycolmap 4.2.1 的輸出能否被 gsplat examples parser 正常讀取**（parser 只要求 `pycolmap>=3.10.0`）— 未验证。
12. 密集點雲（MVS depth）初始化對基線 0.3–1.0 m 的場景是否有淨增益 — gsplat examples 無內建支援，文獻結論不一致（未证实）。

---

需要我把這份內容整理成更精簡的 checklist（或補充某題的原文出处逐字引用）嗎？
