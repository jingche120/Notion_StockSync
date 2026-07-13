# CLAUDE.md

此檔案為 Claude Code (claude.ai/code) 在此專案中工作時提供指引。

## 一定要遵守的規則

1. 刪除任何檔案前，先列出清單與理由讓使用者確認，不要直接刪除。

## 專案概述

一個台灣股市監控服務，於交易時段（台灣時間 09:00–13:30）運行。透過 Fugle API 抓取即時股價、將漲跌以顏色標記更新至 Notion 資料庫、爬取財經新聞，並在股價觸及目標價時對 VIP／VVIP 訂閱者發送 email 警示。

## 常用指令

```bash
# Run the main service (starts initialization, enters market state machine)
python main.py

# Run a single integration test (real API calls — requires .env)
python test_notion_worker.py
python test_single_update.py

# Run test suite (mix of mocked and real tests)
python test_suite.py

# Install dependencies
pip install -r requirements.txt
```

不需要建置步驟。`.venv` 目錄為虛擬環境。

## 環境變數（`.env`）

所有機密在啟動時透過 `python-dotenv` 載入。正式環境（GCP）請改用 `utils/gcp_config.py` 搭配 Secret Manager。

| 變數名稱 | 說明 |
|---|---|
| `STOCK_API_KEYS` | 逗號分隔的 Fugle API 金鑰（一把金鑰對應一個 `API_Worker` 執行緒） |
| `RESERVE_STOCK_API_KEYS` | 第二次重試時使用的備用 Fugle 金鑰 |
| `LATEST_CHANCE_STOCK_API_KEYS` | 第三次重試（最後機會）使用的 Fugle 金鑰 |
| `NOTION_API_KEY_LIST` | 逗號分隔的 Notion API 金鑰（一把金鑰對應一個 `Notion_update_worker` 執行緒） |
| `SENDER_EMAIL` / `SENDER_APP_PASSWORD` | Zoho SMTP 憑證 |
| `MARKET_OPEN_TIME` / `MARKET_CLOSE_TIME` | 覆寫交易時間，用於測試（格式 `HH:MM`） |
| `PRICE_SOURCE` | 抓價來源切換：`fubon`（富邦快照，預設建議）或 `fugle`（舊路，退路） |
| `FUBON_ACCOUNT` / `FUBON_PASSWORD` / `FUBON_CERT_PATH` | 富邦證券登入帳密與 `.pfx` 憑證路徑（`PRICE_SOURCE=fubon` 時需要） |
| `USER_CONFIGS_JSON` | VIP 用戶設定（含 email、Notion `DB_ID`）的 JSON 陣列字串；格式見 `utils/users.example.json`。未設定時退回讀取本機 `utils/users.json`（不進版控） |

## 架構

### 市場狀態機（`main.py`）
系統依序經歷三個狀態：`PRE_MARKET` → `IN_MARKET` → `POST_MARKET`。

- **啟動**：清除 Notion 中前一日的大盤指數資料，將所有股票重置為 `"default"` 顏色與 `"--"` 漲跌幅。
- **PRE_MARKET**：每 60 秒檢查一次，等待至 `MARKET_OPEN_TIME`。
- **IN_MARKET**：重複執行 `run_core_loop()`，直到 `MARKET_CLOSE_TIME`。
- **POST_MARKET**：執行一次 `run_post_market_tasks()`，然後結束程式。

### 核心循環（`run_core_loop`）
循環週期由 `config.CORE_LOOP_DURATION_SECONDS` 決定（目前為 180 秒＝3 分鐘）。每次迭代：
1. 透過 Selenium 爬取新聞與台股加權指數（`news_scraper.py`）
2. 從各 VIP 用戶的 Notion 資料庫抓取其自選股清單（`notion_api_for_vip`）
3. 建立 `N` 個 `API_Worker` 執行緒（N = Fugle 金鑰數量）與 `M` 個 `Notion_update_worker` 執行緒（M = Notion 金鑰數量）
4. 所有工人共用同一個 `task_q`；`API_Worker` 負責生產，`Notion_update_worker` 負責消費
5. 循環視窗內剩餘的時間用來 sleep

### 抓價來源（`PRICE_SOURCE`）
核心循環的「生產者」依 `PRICE_SOURCE` 切換，兩條路產出的 task 封包格式相同（共用 `utils/task_builder.py` 的 `build_task_packet`），下游 `Notion_update_worker` 不受影響：

- **`fubon`（富邦快照，建議）**：`workers/fubon_producer.py` 的 `produce_with_fubon`。一次 `snapshot.quotes(market='TSE')` 抓回全部上市股票（約 1500+ 筆），**單執行緒、不需多金鑰/節流/重試**。富邦 SDK 登入一次後重用連線（module 層級單例）。顏色/漲跌幅直接取自快照（`base_price = lastPrice - change`），並用 module 層級 `_last_price_seen` 做**分鐘級去重**（與上一輪同價就不更新）。
- **`fugle`（舊路，退路）**：`produce_with_fugle` + `workers/api_worker.py`，逐支 quote、受 60/min 限流，靠多金鑰並行與三層備援重試。完整保留，`PRICE_SOURCE=fugle` 即可回退。

### 任務封包格式
一般股票：`[stock_code, price, color, price_change_percent]`
VIP 股票：`[stock_code, price, color, price_change_percent, user_details_list]`

`color` 的值：`"RED"`（漲）、`"GREEN"`（跌）、`"default"`（初始化）。

### 工人並行機制
- `API_Worker`（繼承 `threading.Thread`）：向 Fugle 抓取股價，優先處理 VIP 股票。最多重試 3 次，依序升級使用 `RESERVE_STOCK_API_KEYS`，再升級至 `LATEST_CHANCE_STOCK_API_KEYS`。
- `Notion_update_worker`（繼承 `threading.Thread`）：消化 `task_q`，更新主 Notion 資料庫、更新各 VIP 用戶的個人自選股狀態，若 `email_needed` 為真則寄送 email。
- 失敗回報機制：每個工人各自累積 `failed_*_list`，完成後整包丟入共享的 `queue.Queue`，由 `main.py` 取出並記錄 log。

### 關鍵檔案
- `df_base_data.json` — 前一日收盤價，以股票代碼為 key。由 `run_post_market_tasks` 在收盤後更新。
- `stock_code_to_name_map.json` — 股票代碼對應公司名稱，用於 email 警示內容。
- `utils/users.json` — VIP 用戶設定：`order`（訂單 ID）、`email`、`DB_ID`（Notion 資料庫）、`VVIP` 旗標。此檔案決定哪些用戶會收到 email 通知。
- `utils/config.py` — 所有執行期設定，從環境變數讀取。`VVIP_ORDERS` 在 import 時由 `users.json` 動態產生。
- `logs/info.log` / `logs/debug.log` — 每次執行都會覆寫（`mode='w'`）。

### VIP 與 VVIP 的差異
- **VIP**：股價觸及目標價時，更新 Notion 中的自選股狀態。
- **VVIP**：在 VIP 功能之上，若該行的 `是否通知` 欄位值為 `"email"`，則額外發送 email 警示。
