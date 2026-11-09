# AGENTS.md - Multi-Agent Heterogeneous Build & Coding System

> **系統架構核心宗旨**：
> 本系統採用 **「雲端/頂層協調者（Cloud Coordinator） + 地端主力執行者（Local Ground Worker: Pi + Qwen）」** 的雙層分工架構。
> 透過將高 Token 消耗任務（巨量編譯 Log 消化、程式碼全文檢索、編譯失敗自癒除錯循環、多檔案修改）完全下放至地端無 Token 成本且具 262K Context 的 `qwen3.8-flash-next-nvfp4-nvfp4`，大幅降低雲端模型 Token 開銷，同時保有頂層雲端模型的架構規劃與複雜跨專案協調能力。

---

## 1. 代理人角色與階層分工 (Agent Hierarchy & Roles)

```
        ┌───────────────────────────────────────────────┐
        │        Top-level Coordinator (此角色)          │
        │   (Cloud LLM: Antigravity / Claude / GPT)     │
        │   - 需求拆解、全域依賴編排、跨專案 Pipeline 調度  │
        │   - 派發精準 Brief 給地端 Pi Agent              │
        └───────────────────────┬───────────────────────┘
                                │ 指派任務 (CLI / RPC / Script)
                                ▼
        ┌───────────────────────────────────────────────┐
        │           Local Worker: pi + qwen             │
        │ (pi CLI, provider=ttu-vllm, qwen3.8-flash)    │
        │   - 262K Context, 高速推論 (~150-200 tok/s)   │
        │   - 本地執行編譯、消化幾萬行 Build Logs         │
        │   - C/C++ / Driver / Makefile 修改與修復       │
        │   - 執行 Build-Fix Loop 直至 Exit Code 0      │
        └───────────────────────────────────────────────┘
```

### 1.1 頂層協調者職責 (Top-level Coordinator)
* **身分**：任何閱讀本文件的外部 LLM（如 Antigravity CLI、Claude、ChatGPT、Gemini 等）。
* **核心原則與紅線規範（Mandatory Negative Constraints）**：
  1. 🔴 **嚴禁親自動手修改專案代碼**：協調者**絕對禁止**使用自身的 `edit_file`、`write_file` 或 shell 重定向去直接修改目標專案的任何 C/C++、Makefile、CMake 或驅動代碼。所有修改一律必須派發給 `pi-coder`。
  2. 🔴 **嚴禁親自執行編譯指令**：協調者不得直接在本機跑 `make`、`./build.sh`、`docker run`、`petalinux-build` 或 `vivado`。建置一律交由 `pi` 在地端背景執行，以徹底避免巨量 Build Log 灌入雲端 Context。
  3. 🔴 **嚴禁親自執行 Git Commit / Push**：修復後的暫存、撰寫 Commit 訊息與推送到遠端倉庫，一律交由 `pi-coder` 執行。
  4. 🔴 **嚴禁親自進行技術調研與爬網**：技術規格、架構比較或報錯搜尋，一律派發給 `pi-researcher`。
  5. 🟢 **協調者的唯一職責**：全域架構思考、維護有序的 Todo 清單、派發精準 Brief 給地端 `pi`、驗收 `pi` 的結構化摘要回報、並向人類使用者彙報進度。
  6. 🔒 **嚴格單工序列化（Single-Task Concurrency Lock）**：地端 `pi + qwen` 僅支援單工循序排程，**嚴禁同時併發多個 `pi` 任務**。協調者必須維護一份循序 Todo 清單，前一項完成並確認驗收後，方能解鎖並指派下一項任務。

### 1.2 地端執行者職責 (Local Worker: `pi + qwen`)
* **身分**：由 `pi` CLI 驅動，使用本地 OpenAI 相容端點之 `qwen3.8-flash-next-nvfp4-nvfp4`。
* **核心原則**：
  1. **吞吐大量日誌**：利用地端 262K Context 閱讀編譯日誌，進行篩選與精確錯誤定位（`error:`, `undefined reference` 等）。
  2. **自癒修復閉環（Build-Fix Loop）**：在給定的 Turn Budget 內修改代碼、重新編譯，直到通過。
  3. **產出結構化彙報**：任務結束時，向 Coordinator 提供簡明摘要（修改了哪些檔案、錯誤原因、最終驗證狀態）。

### 1.3 地端角色矩陣與工具配置 (Local Personas & Capability Matrix)
依據 Todo 階段需求，地端 `pi + qwen` 可動態切換為以下 4 種專用角色（受單工序列化規範，同一時間僅啟動一個角色）：

| 角色名稱 | 核心任務 | 工具權限限制 (Tools) | 核心規範與產出 |
| :--- | :--- | :--- | :--- |
| **`pi-coder`** | 代碼撰寫、Build-Fix 自癒閉環、Makefile 修復 | `read,bash,edit,write` | 執行建置與錯誤修復，直至 Exit Code 0，產出修改檔案清單與原因。 |
| **`pi-reviewer`** | 程式碼審查、Git Diff 分析、API 相容性把關 | 唯讀：`read,bash` (限 git diff/log) | **禁止直接修改源碼**。專注於架構規範、內存安全與邊界檢查，產出審查報告。 |
| **`pi-tester`** | 執行回歸測試、模擬、單元測試、覆蓋率 | `read,bash,write` (僅報告) | 觸發測試腳本（如 pytest, ctest, RTL sim），產出 Pass/Fail 測試矩陣。 |
| **`pi-researcher`** | 聯網檢索規格書、解法搜尋、技術文件萃取 | `web_search,fetch_content,read` | 針對罕見編譯錯誤或未知硬體暫存器，至網路蒐集官方文件與 issue 討論。 |

### 1.4 地端推論拓撲與 LLM 通道建立 (`pi-tunnel`)

地端 `pi` 呼叫的 `ttu-vllm` **一律透過 `/home/zzlee/.local/bin/pi-tunnel` 建立的 SSH 隧道**轉發至內部高效能 GPU 運算節點，這是本系統唯一的 LLM 通道建立方式。

* **遠端 GPU 伺服器**：`<GPU_HOST_USER>@<GPU_HOST>`，模型服務為 **Strata**（OpenAI 相容，**非 vLLM**；`Qwen3.8-Flash-Next-NVFP4-GPTQ-Strata`），單卡、KV `int8`、啟用 MTP 投機解碼（`--spec 4`），Context Window 262,144。**遠端監聽 `127.0.0.1:8080`**（不是 8000）。
* **前置條件**：遠端須先啟動 `~/qwen3_8_flash_next_dev/serve-strata.sh`（遠端服務未起時，隧道即使建通也會回傳連線錯誤）。
  * **要能用圖片**：需 `VISION=on ./serve-strata.sh`（腳本第 57 行 `VISION="${VISION:-off}"` → 第 216 行 `--vision "$VISION"`），且模型目錄須有视觉权重（2026-11-08 前缺失，已補）。未帶 `--vision` 時塞圖會回 `HTTP 400: started without the vision encoder`。
  * 🔴 **重啟 strata 會中斷正在運行的 `pi` session**（本機 session 的推論就落在該實例上）。重啟後須確認 `~/.pi/agent/models.json` 的 `input` 含 `"image"`，否則 `pi` 仍會丟棄圖片塊（`read` 回 `tool image omitted`）；改完以 `/model` 重載即可生效，不必重啟。

#### 連接埠轉發對照 (Port Forwarding)
| 本機端點 | 遠端端點 | 服務 / API | 備註 |
| :--- | :--- | :--- | :--- |
| `127.0.0.1:8080` | `:8080` | Strata OpenAI 相容 API（`/v1/chat/completions`） | **實測現況（2026-11-09 校正）**：本機與遠端**同用 8080**，`~/.pi/agent/models.json` 的 `ttu-vllm.baseUrl = http://127.0.0.1:8080/v1`；健康檢查改 `curl -s http://127.0.0.1:8080/v1/models`。（舊檔寫的本機 8000 已不再使用，照舊檔 curl 會得到 HTTP=000 而誤判成「隧道斷了」） |
| `127.0.0.1:11435` | `:11434` | Ollama 相容 API（`/api/chat`，vllm-shim） | **本機 11434 保留給本機 ollama**；strata 不提供 `/api/chat`，此埠目前無效 |

#### 隧道生命週期管理
```bash
pi-tunnel -f                        # 背景常駐（預設 8080→8080）（派工前的標準啟動方式；strata 在遠端 8080）
pi-tunnel                            # 前景執行，Ctrl-C 結束（除錯/看 log 用）
```
> ⚠️ **2026-11-09 校正**：`~/.local/bin/pi-tunnel` 的預設已改成 `API_PORT=8080`（`API_PORT_REMOTE` 預設跟隨它），所以**直接 `pi-tunnel -f` 就是對的**，不再需要手動帶 `API_PORT_REMOTE=8080`。舊文件叫你一定要帶，會讓人以為沒帶就轉到遠端 8000。

**選用的環境變數覆寫**（腳本內皆有預設值，一般不需指定）：

| 變數 | 預設值 | 用途 |
| :--- | :--- | :--- |
| `REMOTE` | `<GPU_HOST_USER>@<GPU_HOST>` | 換 GPU 節點 |
| `API_PORT` | `8080` | **本機** API 埠（2026-11-09 起預設即 8080） |
| `API_PORT_REMOTE` | 同 `API_PORT` | **遠端** API 埠（預設跟隨本機埠，兩邊都是 8080 ⇒ 不必設） |
| `OLLAMA_LOCAL` / `OLLAMA_REMOTE` | `11435` / `11434` | Ollama shim 埠對 |
| `ONLY` | `both` | `openai`（只開 8080）或 `ollama`（只開 11435） |

**腳本內建保護機制**（無須額外操心，但須知其行為）：
1. 啟動前以 `ss -lnt` 檢查目標埠是否已佔用，已佔用即中止並提示「隧道可能已在跑」，**不會重複疊加第二條隧道**。
2. `-f` 模式啟動後延遲 2 秒回驗埠，驗證失敗會自動 `pkill` 回滾，不殘留半死隧道。
3. 透過 `ExitOnForwardFailure=yes` + `ServerAliveInterval=30` + `ServerAliveCountMax=3` 確保轉發失敗即退出、斷線約 90 秒內偵測。

#### 健康狀態檢查與故障排除
```bash
# 1) 通道健康檢查（派工前必要步驟，回傳 qwen3.8-flash-next-nvfp4-nvfp4 即正常）
curl -s http://127.0.0.1:8080/v1/models

# 2) 確認隧道行程與監聽埠
grep -q ":8080 " <<<"$(ss -lnt)" && echo "port 8080 OK"
pgrep -af "ssh -N.*<GPU_HOST>"

# 3) 中斷/重啟隧道（改埠或換節點時使用；pkill 用 ; 而非 &&，未啟動時也能直接拉起）
pkill -f "ssh -N.*<GPU_HOST>"; sleep 1; pi-tunnel -f
```
* **檢查失敗排查順序**：本機無監聽埠 → 未啟動或已斷線，重跑 `API_PORT_REMOTE=8080 pi-tunnel -f`；埠在聽但 `curl` 無回應 → 先確認遠端真的在 8080（`ssh <GPU_HOST_USER>@<GPU_HOST> 'ss -lnt | grep 8080'`），再請人類至遠端啟動 `~/qwen3_8_flash_next_dev/serve-strata.sh`（協調者不得自行長等遠端服務重啟）。
* **模型 ID 為何有兩個 `nvfp4`**：遠端啟動參數是 `--model nvfp4 --family nvfp4`（`serve-strata.sh` 第 50–51 行），而模型本身名稱已含 `NVFP4`，strata 會把量化標籤接在家族名後面，於是變成 `qwen3.8-flash-next-nvfp4-nvfp4`。**此 ID 由伺服器決定，client 端必須照抄**；要消除重複字樣只能改遠端 `MODEL`/`FAMILY` 變數，改本機設定無效。

#### Provider 設定檔來源（`~/.pi/agent/models.json`）
`pi` 的 `ttu-vllm` provider 由本機設定檔定義，派工指令不需重複指定 baseUrl：
* `baseUrl`: `http://127.0.0.1:8080/v1`，`api`: `openai-completions`，`apiKey`: `none`
* Model `qwen3.8-flash-next-nvfp4-nvfp4`：`contextWindow` 262144、`maxTokens` 65536、`reasoning: true`、`input: ["text","image"]`（**2026-11-08 起帶 `--vision` 上線**；此前 strata 自報純文字）、`thinkingFormat: qwen-chat-template`

> **協調者授權例外**：§1.1 紅線禁止協調者親自執行建置/編譯，但 **`pi-tunnel -f` 與上述健康檢查指令為唯一獲准由協調者直接執行的環境維運操作**（僅建立通道、不觸發編譯，不產生巨量 Log）。

---

## 2. Coordinator 調度 `pi + qwen` 的標準通訊規範 (Dispatch Protocol)

### 2.1 標準派工 CLI 模板

```bash
cd <TARGET_PROJECT_PATH> && pi \
  --provider ttu-vllm \
  --model qwen3.8-flash-next-nvfp4-nvfp4 \
  --thinking high \
  --no-context-files \
  -p "<STRUCTURED_TASK_BRIEF>"
```

> **參數說明**：
> - `--provider ttu-vllm`：強制使用本地 OpenAI 相容端點 (`http://127.0.0.1:8080/v1`，隧道轉發至遠端 strata `:8080`)。
> - `--model qwen3.8-flash-next-nvfp4-nvfp4`：strata 回報的完整模型 ID（**不是** `qwen3.8-flash-nvfp4`；舊 ID 會 404）。
> - `--thinking high`：啟動深度思考鏈，確保 C/C++ 語法與 Makefile 邏輯分析嚴謹。
> - `-p`：非互動批次模式，完成任務後退出並回傳結果。
> - `-t read,bash`：（選用的）工具白名單，用於 `pi-reviewer` 等唯讀角色。

### 2.2 派發 Brief 的標準結構 (Structured Task Brief)

```markdown
[ROLE]: <pi-coder | pi-reviewer | pi-tester | pi-researcher>
[TASK]: <具體建置或修復目標>
[TARGET_CWD]: <專案路徑>
[BUILD_COMMAND]: <明確的建置指令>
[BOUNDARIES & RULES]:
1. 允許修改範圍：C/C++ 源碼、標頭檔、Makefile / CMakeLists.txt。
2. Log 分析：若建置失敗，檢查編譯輸出，定位關鍵語法或連結錯誤。
3. 自癒迴圈：修正代碼後重新執行 [BUILD_COMMAND]，直至 Exit Code 0 或達到最大嘗試次數（5 次）。
4. 嚴格限制：禁止使用 git reset --hard 或 rm -rf 未備份的關鍵源碼目錄。
[EXPECTED_OUTPUT]:
完成後請以 Markdown 列出：
- 建置最終狀態 (SUCCESS / FAILED)
- 修改之檔案清單 (Modified Files)
- 關鍵修復原因摘要 (Root Cause & Fix)
```

---

## 3. 自癒迴圈與 Log 消化機制 (Build-Fix Loop & Log Sanitization)

### 3.1 為什麼將 Log 留在地端？
大型 C++ / PetaLinux 專案編譯日誌常超過 5,000 ~ 50,000 行。
* **錯誤做法**：Coordinator 執行 build → 將 5 萬行 log 傳入雲端 Context → Token 爆炸且超出預算。
* **標準做法**：由地端 `pi` 在本機執行 build，輸出直接倒進本機 log 檔案或 `pi` 的內部 Bash 工具中，`pi` 運用地端 262K context 自行提取 `error:`、`fatal error:`、`undefined reference to` 等段落進行修復。

### 3.2 Local Agent 的建置-修復 SOP
1. **Preflight**：確認當前 git status 是否乾淨，記錄當前 commit。
2. **Execute Build**：觸發指定建置指令，導向記錄至臨時 log（例：`build_temp.log`）。
3. **Log Diagnosis**：
   - Exit Code 為 0 → 任務完成，產出成功回報。
   - Exit Code 非 0 → 檢索 log 中最末端及核心的錯誤堆疊。
4. **Patch & Re-verify**：根據錯誤修改對應原始碼或編譯腳本，重新觸發建置並驗證錯誤是否消除，重複直至通過或判定受阻（Blocked）。
5. **Report to Coordinator**：整理修改 diff 摘要回傳給頂層協調者。

### 3.3 跨階段流水線守則 (Pipeline Ordering)
當任務牽涉軟硬體全棧整合時，必須依 **RTL/韌體 → Device Tree → Kernel Driver → 基礎函式庫 → 上層應用** 的相依順序調度：
1. **階段隔離**：嚴禁在下層（驅動/韌體）尚未通過建置與符號凍結前，要求 `pi` 修改上層應用。
2. **產出物傳遞**：協調者須確認上階段 artifacts（`.so`、標頭檔套件、`.ko`、BOOT.BIN）已更新，再派工下一階段。

---

## 4. 安全護欄與人機審批點 (Safety Guardrails & HITL)

儘管地端 `pi` 被授權修改代碼，以下操作**必須終止自動迴圈並回報給人類使用者審批**：

1. **硬體燒錄與實體抹除**：執行 `flash_*.sh`、SPI Flash 燃錄、MCU 韌體更新、或寫入板卡 eMMC。
   *原因*：若 bitstream 或 MCU 韌體因 bug 鎖死 JTAG 或磚化板卡，需硬體重置。
2. **破壞性檔案與版本操作**：嚴格禁止未備份執行 `git reset --hard`、`git clean -fdx`、或帶萬用字元的 `rm -rf`。
3. **無窮修復死鎖（Circuit Breaker）**：單一建置修復迴圈上限為 **5 次嘗試**。若同一編譯錯誤連續 5 次修復失敗，`pi` 必須中止，將最後的根本錯誤（Root Cause）與已嘗試方案彙報給 Coordinator，交由人類決策。

---

## 5. 協調者快速檢核清單 (Coordinator Quick Checklist)

在呼叫 `pi + qwen` 前，請自我檢查：
- [ ] 目標路徑是否為真實專案路徑？
- [ ] LLM 通道是否已建立且健康 — 先 `curl -s http://127.0.0.1:8080/v1/models`；若無回應則 `API_PORT_REMOTE=8080 pi-tunnel -f` 重建後再驗一次（見 §1.4）。
- [ ] 建置指令是否包含必要的 Docker 包裝腳本（如 `./scripts/docker-run.sh`）或環境變數？
- [ ] 是否給予了明確的自癒修復目標，而不是含糊的「請幫我看看」？
- [ ] 是否要求地端 Agent 提供結構化的結果彙報以節省 Coordinator 的理解成本？
- [ ] 當前是否已有正在運行的 `pi` 任務？（嚴格維持單工序列化，前一任務未結束或未驗收前禁止派發新任務）
- [ ] 是否已將待辦事項拆解為有序的 Todo 清單？
