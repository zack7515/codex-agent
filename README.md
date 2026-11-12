# Codex Agent

用網頁操作 Codex coding agent，模型改接自架的模型伺服器（例如 Strata），不連 OpenAI 的雲端服務。

```
瀏覽器（codex_agent.html）
   │  HTTP / SSE
   ▼
codex_agent_server.py（FastAPI）
   │  openai-codex Python SDK
   ▼
codex app-server（本機子程序，實際讀寫檔案、執行指令）
   │  Responses API
   ▼
自架模型伺服器（Strata 等）
```

> ⚠️ Codex 執行的指令和檔案修改，都發生在**執行 `codex_agent_server.py` 的這台電腦**上。

## 安裝

需要 Python 3.10 以上。

```bash
pip install -r requirements.txt
```

`openai-codex` 會一併安裝 codex 執行檔，不需要另外裝 Codex CLI。目前測過的版本是 **openai-codex 0.167.1**（升級時的注意事項見[已知限制](#已知限制)）。

## 啟動

```bash
python codex_agent_server.py                    # 只有本機能開：http://127.0.0.1:8765
python codex_agent_server.py --port 9000        # 換 port
python codex_agent_server.py --host 0.0.0.0     # 讓區網其他電腦用 http://<這台的 IP>:8765 開
```

用 `--host 0.0.0.0` 開放給其他電腦前請注意：**這個服務沒有登入驗證**，任何連得到的人都能讓 Codex 在這台電腦上執行指令。只在信任的內網使用，或自行在前面加反向代理和驗證。

## 使用方式

1. 打開網頁，展開右側欄的「連線設定」，填入模型伺服器：
   - **Base URL**：要包含 `/v1`，例如 `http://192.168.31.120:8080/v1`
   - **API key**：伺服器沒設就留空
   - **模型名稱**、**上下文長度**：依模型伺服器填寫
2. 在中間的輸入框打任務，按 **Enter** 送出（**Shift+Enter** 換行）。第一次送出時，程式會自己啟動 Codex 並開新對話。
3. 右上角的狀態燈：

   | 狀態 | 意思 |
   |---|---|
   | 灰：未連線 | Codex 還沒啟動 |
   | 黃：檢查模型伺服器… / 等你回答 | 正在確認連線，或 Codex 在等你按允許、回答問題 |
   | 綠：可以送任務了 / 正在跑… | 模型伺服器連得到 |
   | 紅：模型伺服器連不上 | 連不到 Base URL 的 `/models`。滑鼠移到燈上可以看原因 |

   狀態燈每 30 秒、每回合結束時都會重新確認一次模型伺服器。

### 畫面說明

- **左側：歷史對話**。點標題可以接續那輪對話；✏ 改標題，✕ 刪除（Codex 那邊會歸檔）。
- **中間：對話**。Codex 的回覆會顯示成 Markdown；指令輸出、思考內容預設收起來，點開才看得到。
  - **思考強度**：關閉 / low / medium（預設）/ high / xhigh，只影響下一次送出。
  - **context 圓圈**：這輪對話用掉多少上下文，點開可以手動壓縮。
  - **補充指示**和**中斷**：只在執行中出現。補充指示會在不中斷的情況下追加指令給 Codex。
- **右側：設定**。平常不用動，連線有問題時才需要。
  - **開始**：測試連線、手動啟動或停止 Codex、清空畫面。
  - **新對話自動用的設定**：工作資料夾、Sandbox、Approval、給模型的規則。
  - **本回合參數**：只影響下一次送出的 Sandbox，以及要求 JSON Schema 格式的輸出。

### 打 `/` 找功能和 skills

在空的輸入框打 `/` 會跳出選單，繼續打字可以篩選（比對名稱和說明，中文也可以）。用 ↑↓ 選、Enter 或 Tab 確定、Esc 關閉，也可以直接用滑鼠點。

**內建指令**

| 指令 | 作用 |
|---|---|
| `/new` | 開新對話 |
| `/compact` | 壓縮這輪對話 |
| `/stop` | 中斷正在跑的回合 |
| `/clear` | 清空中間的畫面 |
| `/skills` | 重新讀取並列出所有可用的 skills |
| `/review` | 填入「審查未 commit 變更」的任務範本，不會自動送出 |
| `/settings` | 打開右側設定 |
| `/help` | 列出所有指令 |

**Skills**

選單下半部是 Codex 載入的 skills，來源是 `~/.codex/skills/`、專案裡的 skills，以及已安裝的 plugin。

- **指定 skill**：選了之後，輸入框上方會出現 `$名稱` 標籤，送出時會**明確指定** Codex 使用這個 skill，不靠模型自己判斷。可以一次選好幾個。
- **移除標籤**：按標籤上的 ✕，或在空的輸入框按 Backspace。
- **只選 skill 不打字**：也可以直接送出。
- **新增 skill 後**：用 `/skills` 重新讀取。

沒有指定 skill 時，Codex 照樣看得到所有 skills，會自己判斷要不要用。

### Approval：Codex 要求核准、跳出問題

「新對話自動用的設定」裡的 **Approval** 有三種：

| 選項 | 行為 |
|---|---|
| **詢問我**（預設） | 需要超出沙盒的權限時，對話裡會跳出卡片讓你選擇允許、這次對話都允許、拒絕或拒絕並停止 |
| auto_review | 由 Codex 自己的審核機制決定，不會問你 |
| deny_all | 一律拒絕，Codex 只能在沙盒內工作 |

模型也可以主動丟出問題和選項請你選（Codex 的 `request_user_input` 工具），一樣以卡片顯示。

- **自動拒絕**：卡片 **10 分鐘**沒回答會自動拒絕；按「中斷」或關掉頁面也會視為拒絕。
- **補充指示會延後**：等你回答的期間，「補充指示」要等你回答完才會生效。

### Sandbox

| 選項 | 能做的事 |
|---|---|
| read-only | 只能讀檔 |
| workspace-write（預設） | 可以在工作資料夾裡寫檔 |
| full-access | 沒有限制，請小心使用 |

## 紀錄與除錯

每個回合的時間軸（首事件延遲、首 token、總時長、各類事件次數）都會寫進 `.agent/logs/YYYY-MM-DD.jsonl`，網頁上不顯示，需要時直接查：

```bash
grep -h error .agent/logs/*.jsonl | tail                                   # 找錯誤
tail -n 5 .agent/logs/$(date +%F).jsonl | python -m json.tool --json-lines # 看今天最後 5 回合
```

預設只記錄事件名稱和時間。要連同完整內容一起記錄，啟動前設定環境變數：

```bash
CODEX_AGENT_LOG_PAYLOADS=1 python codex_agent_server.py
```

Codex 自己的對話紀錄在 `~/.codex/sessions/`。

### 常見問題

| 狀況 | 可能原因 |
|---|---|
| 紅燈，原因是 `ConnectError` | IP 或 port 錯誤、模型伺服器沒有聽 `0.0.0.0`、被防火牆擋 |
| 測試連線出現 401 | API key 錯誤或沒填 |
| 測試連線出現 403 | 模型伺服器檢查 Host，改用 IP 或設定 API key |
| 深度測試時 `/responses` 出現 404 | 模型伺服器沒有實作 Responses API，Codex 無法使用 |
| 送出後回 409 | 已經有回合在跑。模型伺服器一次只處理一個請求，等它結束或按「中斷」 |
| 很久才有第一個字 | 長 prompt 的 prefill。串流逾時預設 600 秒，可以在「連線設定 → 進階」調整 |

## HTTP API

網頁用的就是這些端點，也可以用 curl 直接呼叫。

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/` | 網頁 |
| POST | `/api/ping?deep=true` | 不經過 Codex 直接測模型伺服器（`/models`，`deep` 時再打 `/responses`） |
| POST | `/api/connect` / `/api/disconnect` | 啟動 / 停止 codex app-server |
| GET | `/api/health` | Codex 是否啟動、模型伺服器是否連得到 |
| GET | `/api/status` | 目前設定、進行中的回合、上回合計時 |
| GET / POST | `/api/threads` | 列出 / 新建對話 |
| POST | `/api/threads/{id}/resume` | 接續對話 |
| GET | `/api/threads/{id}/history` | 讀回對話內容 |
| GET | `/api/skills?cwd=&reload=true` | 列出 Codex 載入的 skills |
| POST | `/api/threads/{id}/name` / `compact` / `archive` | 改標題 / 壓縮 / 歸檔 |
| POST | `/api/threads/{id}/turn` | 送出任務，回應是 SSE 事件串流 |
| POST | `/api/turns/{id}/steer` / `interrupt` | 補充指示 / 中斷 |
| POST | `/api/requests/{id}` | 回答核准或提問，body：`{"result": {...}}` |

## 給其他程式用：OpenAI 相容介面

別人已經有自己的對話框、只想用這台的 Codex 加模型時，另外跑 `openai_gateway.py`。它提供 OpenAI 格式的 `/v1/chat/completions`，對方把 Base URL 換成這台就能用，不用改程式。

```
對方的 chat UI ──/v1/chat/completions + token──▶ openai_gateway.py（8766）──▶ codex_agent_server.py（8765，只聽本機）
```

兩個服務都會佔住終端機，要**開兩個終端機**各跑一個：

```bash
# 終端機 1：後端照常啟動，不用開放給外面
python codex_agent_server.py --port 9000
```

```bash
# 終端機 2：gateway
python openai_gateway.py --host 0.0.0.0 --token pwd --llm-url http://192.168.31.120:8080/v1 --llm-model strata --cwd /path/to/project
```

所有參數（`python openai_gateway.py --help` 也看得到）：

| 參數 | 意思 | 預設 |
|---|---|---|
| `--host` / `--port` | gateway 聽的位置 | `127.0.0.1` / `8766` |
| `--token` | 對方要帶的 token（單一 token） | 無 |
| `--users` | 多人名單檔，見下面「多人使用」 | 無 |
| `--cwd` | Codex 的工作資料夾（多人模式是各人資料夾的上層） | 見下面 |
| `--sandbox` | read-only / workspace-write / full-access | workspace-write |
| `--llm-url` / `--llm-model` / `--llm-key` | 模型伺服器；有給的話，Codex 還沒啟動時 gateway 會自己連線 | 無（要先在網頁上連線） |
| `--agent-url` | `codex_agent_server.py` 的位置 | `http://127.0.0.1:8765` |

每個參數也可以用環境變數設（例如 `CODEX_GATEWAY_TOKEN`、`LLM_BASE_URL`，`--help` 裡有對照），命令列參數優先。`--token` 會留在命令列歷史和工作管理員裡，正式使用建議改用 `--users` 名單檔。


對方的設定：

| 欄位 | 值 |
|---|---|
| Base URL | `http://<這台IP>:8766/v1` |
| API key | 發給他的 token |
| 模型 | `codex-agent` |

### 在對方的對話框使用 skills

- **列出 skills**：訊息只打 `/skills`，會直接回覆可用的 skills 清單。這個回覆由 gateway 產生，不經過模型，也不佔排隊。
- **指定 skill**：訊息裡打 `$名稱`，例如 `$review-agent 幫我審查最近的修改`。名稱對得到的，會明確指定 Codex 使用那個 skill，跟網頁的 `/` 選單一樣；對不到的就當普通文字。一次可以指定好幾個。
- **不指定也可以**：Codex 看得到所有 skills，會自己判斷要不要用。

### 多人使用：每人一個 token 和資料夾

建一個名單檔 `gateway_users.json`（已在 .gitignore，不會被 commit）：

```json
{ "alice": "至少16字的亂碼-aaaa", "bob": "至少16字的亂碼-bbbb" }
```

```bash
python openai_gateway.py --host 0.0.0.0 --users gateway_users.json --cwd /srv/codex-workspaces
```

- **自動分資料夾**：gateway 依 token 認人，alice 的對話都在 `<--cwd>/alice/` 裡工作，資料夾會自動建立。沒給 `--cwd` 時是專案裡的 `workspaces/`。
- **對話不互通**：alice 和 bob 的對話各自分開，就算送一樣的歷史也不會接到對方的。
- **新增或移除人**：改名單檔後重啟 gateway。
- **名單格式**：名稱只能用英數字、`_`、`-`；token 至少 16 字且不能重複，不符合就拒絕啟動。
- **只有一組 token 時**：可以改用 `--token`，不分資料夾，工作資料夾就是 `--cwd`；沒給 `--cwd` 時是啟動 `codex_agent_server.py` 時所在的資料夾。
- ⚠️ **沙盒只限制寫入、不限制讀取**：Codex 讀得到執行帳號能讀的所有檔案，包括別人的資料夾。所以要用專用帳號跑（見下一節），讓它至少讀不到你的檔案。

### 用專用帳號跑（Windows）

1. 用系統管理員開 PowerShell，建一個**一般使用者**帳號（不要加進 Administrators）：
   ```powershell
   net user codex-svc <密碼> /add
   ```
2. 專案放在 `codex-svc` 讀得到的位置，例如 `C:\codex-agent`，**不要放在你自己的 `C:\Users\<你>` 底下**。Python 選「為所有使用者安裝」，套件用 `codex-svc` 身分裝一次。
3. 用 `codex-svc` 身分開一個視窗，在裡面啟動兩個服務：
   ```powershell
   runas /user:codex-svc powershell
   # 在新視窗裡：
   cd C:\codex-agent
   pip install -r requirements.txt
   Start-Process python codex_agent_server.py
   python openai_gateway.py --host 0.0.0.0 --users gateway_users.json --cwd C:\codex-workspaces --llm-url http://192.168.31.120:8080/v1 --llm-model strata
   ```
4. 防火牆只開 8766，8765 不要開。要開機自動啟動，就用「工作排程器」設定以 `codex-svc` 身分執行，並勾選「不論使用者登入與否均執行」。

Windows 預設只有本人和系統管理員能讀 `C:\Users\<你>`，所以一般帳號的 `codex-svc` 讀不到你的檔案。但 `D:\` 這類其他磁碟，以及 `C:\` 底下自己建的資料夾，預設通常 Users 群組都讀得到，有機密資料的話要另外調整權限。`codex-svc` 也有自己的 `C:\Users\codex-svc\.codex`，skills 和設定要另外放一份進去。

Linux 的作法相同：`sudo useradd -m codex-svc`，再用 `sudo -u codex-svc -H` 啟動兩個服務。

### 其他說明

- **token 必填**：`--host` 不是本機時，`--users` 和 `--token` 都沒給就不給啟動。
- **對話自動對應**：同一段對話接著問會沿用同一個 Codex 對話，只送最後一句。改過舊訊息、gateway 或後端重啟過，就開新對話，並把先前的內容當參考一起送。
- **核准由 Codex 自己決定**：固定用 `auto_review`，不會跳出卡片。對方只會看到回覆文字，看不到指令執行過程。
- **一次只跑一個回合**：所有人共用一個佇列，別人正在用時會回 **429**，大部分 chat UI 會顯示「請稍後再試」。
- **斷線就中止**：對方中途關掉，這個回合會跟著中止。
- **忽略的參數**：`temperature`、`tools`、圖片這類 OpenAI 參數。

## 檔案

| 路徑 | 內容 |
|---|---|
| `codex_agent_server.py` | 後端 |
| `codex_agent.html` | 網頁（單一檔案，不需要建置，也不用連外網） |
| `openai_gateway.py` | OpenAI 相容轉接層（給其他 chat UI 用） |
| `requirements.txt` | Python 套件 |
| `.agent/` | 中間產物與紀錄（`logs/`） |

## 已知限制

- **SDK 內部介面**：openai-codex SDK 沒有公開「交給使用者核准」、自訂核准處理和列出 skills 的介面，程式用了 SDK 的三處內部函式，在 `codex_agent_server.py` 裡以 `ponytail:` 註解標示。**升級 SDK 後，如果核准卡片不再出現或 `/` 選單沒有 skills，先檢查這三處。**
- **提問工具是實驗功能**：模型丟出選項請你選，用的是 Codex 標為 under development 的功能（`features.default_mode_request_user_input`）。模型會不會真的發問，取決於模型本身。
- **只有一個工作階段**：同一時間只跑一個回合，也只保留一個 Codex 程序。多個瀏覽器分頁共用同一份狀態。
