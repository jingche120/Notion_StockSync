# Notion StockSync — 維運手冊（RUNBOOK）

> 給未來的自己：這份 RUNBOOK 是「回來後快速回憶程式怎麼跑、怎麼重啟、怎麼排查」的維運入口。
> 專案的對外簡介（架構、技術挑戰）請看 [README.md](README.md)。
> 想看「重啟前怎麼逐項驗證金鑰與 ID」，請看 [restart_checklist.md](restart_checklist.md)（本地檔，未上傳 GitHub）。
> 想看「Claude Code 在此專案的工作守則」，請看 [CLAUDE.md](CLAUDE.md)。

---

## 這個專案在做什麼

一個在台灣股市交易時段（09:00–13:30）運行的監控服務，每 **1 分鐘**做一輪：

1. **抓即時股價**，把漲跌以紅／綠顏色 + 漲跌幅更新到 **Notion 主資料庫**。抓價來源可切換（見下方 `PRICE_SOURCE`）：**富邦 snapshot（建議，一次抓全市場）** 或 Fugle 逐支輪詢（退路）。
2. **爬財經新聞與台股加權指數**（Selenium 開無頭 Chrome），寫回 Notion。
3. **VIP／VVIP 到價通知**：抓每位 VIP 用戶自己的 Notion 自選股清單，股價碰到目標價時更新狀態，VVIP 還會額外寄 email 警示。

收盤後做一次結算，把當天收盤價寫回基準檔，當作隔天的比較基準。

> **⚠️ 目前實際狀態（2026-07）**：程式碼中 **新聞爬蟲（Step 1）與 VIP／email（Step 2）已暫時停用**，本輪只更新主資料庫的即時股價。要重新啟用，把 [main.py](main.py) 中對應的註解區塊解開即可（詳見下方各段說明）。
>
> **抓價已改用富邦**：富邦 snapshot 已實作完成（`workers/fubon_producer.py`），一次呼叫抓回全市場、不受 60/min 限流。Fugle 逐支輪詢保留為退路（`PRICE_SOURCE=fugle`）。注意 `config.py` 的 `PRICE_SOURCE` **預設值仍是 `fugle`**，要用富邦請在 `.env` 明確設 `PRICE_SOURCE=fubon`。

---

## 60 秒看懂執行流程

進入點是 [main.py](main.py)，從 `if __name__ == "__main__"` 開始：

```
系統啟動
  │
  ├─ 載入 df_base_data.json（昨日收盤價，當作今天的漲跌比較基準，也是「要監控哪些股票」的清單來源）
  │
  ├─ 初始化 Notion：
  │    • 刪掉 Notion 上昨天的大盤指數資料（封存舊頁面）
  │    • 把主資料庫所有股票重置成 default 顏色、漲跌幅 "--"
  │      （多執行緒，一把 Notion 金鑰一個 initialization_worker）
  │
  ├─ 進入「市場狀態機」get_current_state()：
  │
  │   PRE_MARKET（盤前）  ── 每 60 秒檢查一次，等到開盤時間
  │        │
  │   IN_MARKET（盤中）   ── 重複跑 run_5_minute_core_loop() 直到收盤
  │        │
  │   POST_MARKET（盤後） ── 跑一次 run_post_market_tasks() 後結束程式
  │
  └─ 系統關閉
```

### 核心迴圈 `run_5_minute_core_loop()`（[main.py](main.py)）

> 函式名雖叫 `run_5_minute_core_loop`，但實際循環週期已改為 **1 分鐘**（`CORE_LOOP_DURATION_SECONDS = 60`）。新聞已與股價更新解耦，最多每 `NEWS_SCRAPE_INTERVAL_SECONDS`（300 秒）才跑一次。

```
[Step 1] 爬新聞 + 加權指數  → 寫進 Notion          (news_scraper.py) ← ⚠️ 目前停用（整段註解）
[Step 2] 抓每位 VIP 的自選股清單                    (notion_api_for_vip) ← ⚠️ 目前停用
[Step 3] 把所有股票分成 VIP 名單 / 普通名單
[Step 4] 開 M 個 Notion_update_worker（M = Notion 金鑰數量）  ← 消費者，先啟動
[Step 5] 依 PRICE_SOURCE 選生產者，把股價封包灌進 task_q：
              PRICE_SOURCE=fubon → produce_with_fubon()  單執行緒、一次抓全市場
              PRICE_SOURCE=fugle → produce_with_fugle()  開 N 個 API_Worker 逐支抓
         │
         └─ 生產者與消費者共用一個 task_q 佇列：
              生產者抓到股價 → 打包（build_task_packet）丟進 task_q
              Notion_update_worker 從 task_q 取出 → 更新 Notion / 寄信
         │
         生產者跑完 → 對佇列放 M 個 None 當結束信號
         → 等所有 Notion_update_worker 收工
         → 收集失敗清單寫進 log
         → 用迴圈剩下的時間 sleep（湊滿 1 分鐘）
```

### 生產者／消費者

抓價「生產者」依 `PRICE_SOURCE` 二擇一，兩條路產出的 task 封包格式相同（共用 [utils/task_builder.py](utils/task_builder.py) 的 `build_task_packet`），下游消費者不受影響。

| 角色 | 檔案 | 職責 |
|---|---|---|
| 生產者（富邦，**建議**） | [workers/fubon_producer.py](workers/fubon_producer.py) | `produce_with_fubon`：一次 `snapshot.quotes()` 抓回全市場（~1500+ 筆），**單執行緒、不需多金鑰/節流/重試**。富邦 SDK 登入一次後重用連線（module 單例）。顏色/漲跌幅直接取自快照（`base_price = lastPrice - change`），並用 module 層級 `_last_price_seen` 做**分鐘級去重**（與上一輪同價就不更新）。 |
| 生產者（Fugle，退路） | [workers/api_worker.py](workers/api_worker.py) | `produce_with_fugle` + `API_Worker`：向 Fugle 逐支抓股價（**VIP 優先**）。每抓一支 **節流 sleep `FETCH_INTERVAL_SECONDS`（預設 1.1 秒）** 壓在 60/min 以下。最多重試 3 次：第 2 次升級 `RESERVE_STOCK_API_KEYS`，第 3 次 `LATEST_CHANCE_STOCK_API_KEYS`。`404`（下市）跳過，`429`（限流）退避後重試。 |
| 消費者 | [workers/notion_worker.py](workers/notion_worker.py) | `Notion_update_worker`：從 `task_q` 取任務，更新主資料庫顏色/漲跌幅；若是 VIP 任務，更新該用戶自選股狀態；若 VVIP 且該行「是否通知」= `email`，就寄 email（最多重試 3 次）。 |

### 任務封包格式（worker 之間傳遞的資料）

- 普通股票：`[股票代碼, 價格, 顏色, 漲跌幅]`
- VIP 股票：`[股票代碼, 價格, 顏色, 漲跌幅, user_details_list]`（多一個元素）

`顏色` 的值：`"RED"`（漲）、`"GREEN"`（跌）、`"default"`（初始化）。

---

## ⚠️ 已知限制與目前對策（很重要）

### 1.（僅 `PRICE_SOURCE=fugle` 退路才有）Fugle 免費版限流是「每帳號 60 次/分鐘」
> **已用富邦 snapshot 解掉**：`PRICE_SOURCE=fubon` 一次抓全市場、限流 300/min 用不完，以下限制只在退回 Fugle 逐支輪詢時才需要在意。
- 不是 per 金鑰，是 **per 帳號**（同帳號多開金鑰會共用同一個 60/min）。
- 因為免費版**不支援 snapshot（一次抓全市場）**，只能逐支 `quote`，~1287 支 = ~1287 次呼叫。
- **對策**：`API_Worker` 用 `FETCH_INTERVAL_SECONDS = 1.1` 節流（~54/min），每個 worker 用一把（=一帳號）金鑰，1287 支分散到 N 把金鑰。
- 三階層金鑰：`STOCK_API_KEYS`（主力，多 worker）→ `RESERVE_STOCK_API_KEYS`（重試第 2 階，1 把即可）→ `LATEST_CHANCE_STOCK_API_KEYS`（重試第 3 階，1 把即可）。

### 2. 主資料庫「只更新、不新增」
- 更新邏輯是「**先查既有頁面 → 再更新**」，**從不 create**。
- 所以**重建後的空資料庫，跑 main.py 不會自動把股票餵進去**，必須先用 [seed_main_db.py](seed_main_db.py) 種入。
- 目前主資料庫已種入 **1287 支上市股票**。

### 3. Notion 已遷移到新版 API（data sources）
- 使用的 notion-client 已移除舊的 `databases.query`，全專案改用 **`data_sources.query` / `data_sources.retrieve`**。
- `config.py` 裡的 `MAIN_DATABASE_ID`、`TPE_INDEX_ID` 存的是 **data source id**（不是 database id，也不是網址）。
- `pages.create` 的 parent 用 `{"type": "data_source_id", "data_source_id": ...}`。

---

## Notion 資料庫結構（重建時照這個建）

### 主資料庫（即時股價，`MAIN_DATABASE_ID`）
| 欄位 | 型別 | 備註 |
|---|---|---|
| `股票名稱` | Title（標題） | 公司名稱 |
| `股票代碼` | 文字（rich_text） | 查詢 key，**不可用數字/標題** |
| `標記` | 多選（multi_select） | 必須有選項 `所有股票`，每筆都要勾 |
| `即時價格` | 文字（rich_text） | 用文字才能套紅綠顏色 |
| `漲跌幅` | 文字（rich_text） | 同上 |

### 大盤指數資料庫（`TPE_INDEX_ID`）
| 欄位 | 型別 |
|---|---|
| `時間點` | Title（標題） |
| `值` | 文字（rich_text） |
| `建立時間` | 日期（date） |

---

## 怎麼跑起來

```bash
# 1. 進虛擬環境
source .venv/bin/activate

# 2. 安裝依賴（第一次或換機器才需要）
pip install -r requirements.txt

# 3. 確認 .env 設定完整（見下方表格）

# 4.（只有空資料庫第一次需要）種入所有股票
python seed_main_db.py

# 5. 啟動主服務
python main.py
```

> 需要本機有 Chrome，因為新聞爬蟲用 Selenium 開無頭 Chrome（**新聞目前停用時可不理**）。

> **平台提醒**：本專案近期已移機到 **Windows**。下方的 `source .venv/bin/activate`、`tmux`、`caffeinate` 都是 macOS／Linux 指令；在 Windows 請改用 `.venv\Scripts\activate`，背景長跑改用工作排程器或直接開一個保持開啟的終端機。

### 在背景跑（tmux，macOS／Linux）

```bash
brew install tmux                       # 只需一次
tmux new -s marquee                     # 建立 session
# 在裡面： source .venv/bin/activate && caffeinate -i python main.py
# 按 Ctrl+b 然後 d  → detach（程式繼續跑）
tmux attach -t marquee                  # 隨時接回來看
tmux kill-session -t marquee            # 停掉整個 session
```

> `caffeinate -i` 防止 Mac 閒置睡眠；但**闔蓋在電池模式仍會睡** → 盤中請接電源、不要闔蓋。長期穩定跑建議放 GCP。

### 測試／除錯

```bash
python test_single_update.py   # 真實更新主資料庫中單一股票（驗證 Notion 讀寫）
python test_notion_worker.py   # 真實驗證 Notion 更新流程
```

要在「非交易時段」測試整個流程：在 `.env` 設定 `MARKET_OPEN_TIME` / `MARKET_CLOSE_TIME` 為「現在的幾分鐘後」，再 `python main.py`。**正式上線前記得把這兩行移除**，否則市場時間會被覆寫。

---

## 環境變數（`.env`）

啟動時由 `python-dotenv` 載入。正式環境（GCP）改用 [utils/gcp_config.py](utils/gcp_config.py) 搭配 Secret Manager。

| 變數 | 說明 |
|---|---|
| `PRICE_SOURCE` | 抓價來源：`fubon`（富邦快照，**建議**）或 `fugle`（逐支輪詢，退路）。**預設 `fugle`**，要用富邦需明確設 `fubon`。 |
| `FUBON_ACCOUNT` / `FUBON_PASSWORD` | 富邦證券登入帳密（`PRICE_SOURCE=fubon` 時需要） |
| `FUBON_CERT_PATH` | 富邦 `.pfx` 憑證路徑（例：`information/client.pfx`；`.pfx` 已 gitignore 不進版控） |
| `FUBON_SNAPSHOT_MARKETS` | 富邦快照要合併查詢的市場別，逗號分隔（預設 `TSE,OTC,ESB,TIB`；含創新板 TIB，只查 TSE 會漏掉如 8487） |
| `SIMULATE_PRICE_CHANGE` | **僅供測試**：`true` 時讓富邦路每檔 lastPrice 有 0.5 機率 +1，模擬盤中跳動以測去重。正式環境務必拿掉或設 `false`。 |
| `STOCK_API_KEYS` | 逗號分隔的 Fugle 金鑰（**一把金鑰 = 一個 `API_Worker` = 一個帳號的 60/min**）。僅 `PRICE_SOURCE=fugle` 時使用。 |
| `RESERVE_STOCK_API_KEYS` | 第 2 次重試用的備用 Fugle 金鑰（**1 把即可**，僅 fugle 路） |
| `LATEST_CHANCE_STOCK_API_KEYS` | 第 3 次重試（最後機會）用的 Fugle 金鑰（**1 把即可**，僅 fugle 路） |
| `NOTION_API_KEY_LIST` | 逗號分隔的 Notion 金鑰（**一把金鑰 = 一個 `Notion_update_worker` 執行緒**） |
| `RESERVE_NOTION_API_KEY_LIST` | 備用 Notion 金鑰（查詢/更新失敗時隨機選用） |
| `SENDER_EMAIL` / `SENDER_APP_PASSWORD` | Zoho SMTP 寄信憑證 |
| `MARKET_OPEN_TIME` / `MARKET_CLOSE_TIME` | 覆寫交易時間，**僅供測試**（格式 `HH:MM`） |

---

## 關鍵檔案地圖

| 檔案 | 用途 |
|---|---|
| [main.py](main.py) | 進入點：初始化 + 市場狀態機 + 五分鐘迴圈 + 盤後結算 |
| [news_scraper.py](news_scraper.py) | Selenium 爬新聞與加權指數，寫回 Notion（**目前停用**） |
| [workers/fubon_producer.py](workers/fubon_producer.py) | 生產者（**建議**）：富邦 snapshot 一次抓全市場、登入單例、分鐘級去重 |
| [workers/api_worker.py](workers/api_worker.py) | 生產者（退路）：抓 Fugle 股價（含節流/404/429 處理）、打包任務 |
| [workers/notion_worker.py](workers/notion_worker.py) | 消費者：更新 Notion、寄 email |
| [utils/task_builder.py](utils/task_builder.py) | `build_task_packet`：兩條抓價路共用的 task 封包打包器（確保下游格式一致） |
| [utils/config.py](utils/config.py) | 所有執行期設定。**Notion data source id 寫死在這裡**（`MAIN_DATABASE_ID`、`TPE_INDEX_ID`）；`VVIP_ORDERS` 由 `users.json` 動態產生 |
| [utils/helpers.py](utils/helpers.py) | 共用工具函式（更新 Notion、抓 VIP 資料、查股名、`get_price_safely` 安全取價等） |
| [utils/mail_sender.py](utils/mail_sender.py) | `EmailSender` 類別，Zoho SMTP 寄信 |
| [utils/users.json](utils/users.json) | VIP 用戶設定：`order`、`email`、`DB_ID`、`VVIP` 旗標。**決定誰會收到通知** |
| [utils/gcp_config.py](utils/gcp_config.py) | 正式環境用 GCP Secret Manager 取代 `.env` |
| [seed_main_db.py](seed_main_db.py) | **一次性**把所有股票種入空的主資料庫（重建後第一次才需要） |
| [fugle_ratelimit_test.py](fugle_ratelimit_test.py) | 測試 Fugle 限流是 per 帳號還是 per 金鑰（除錯用小工具） |
| `df_base_data.json` | 前一日收盤價（key = 股票代碼）。**同時是「要監控哪些股票」的清單來源**。盤後由 `run_post_market_tasks` 更新 |
| `stock_code_to_name_map.json` | 股票代碼 → 公司名稱（巢狀：`{"stock_code_to_name_map": {...}}`），用於 email 內容 |
| `logs/info.log` / `logs/debug.log` | 每次執行都會 **覆寫**（`mode='w'`）。除錯先看這兩個 |

### VIP vs VVIP（⚠️ 目前暫時停用）

> [main.py](main.py) 的 Step 2 已把 VIP 抓取與 email 暫時關閉，本輪只更新主資料庫。要重新啟用，解開該段註解即可（`master_vip_list` 會恢復非空、封包補上第 5 元素、寄信自動生效）。

- **VIP**：股價碰到目標價時，只更新 Notion 中該股票的狀態。
- **VVIP**：在 VIP 之上，若該行「是否通知」欄位值為 `"email"`，則額外寄 email 警示。

---

## 未來要做的事（Roadmap）

依優先序：

1. ✅ **改用富邦證券 snapshot 抓價 —— 已完成**
   已實作 `workers/fubon_producer.py`：`snapshot.quotes()` 一次抓回全市場（限流 300/min 用不完），設 `PRICE_SOURCE=fubon` 即啟用。Fugle 逐支輪詢保留為退路。剩餘可做：把 `config.py` 的 `PRICE_SOURCE` 預設值改成 `fubon`、驗證穩定後移除 Fugle 舊路。

2. **重新啟用新聞爬蟲與 VIP／email**
   兩者目前在 `main.py` 中以註解暫時停用（移機到 Windows 時先略過）。確認環境（Chrome/Selenium、憑證）就緒後解開註解恢復。

3. **用權威清單重建股票宇宙**
   開盤時用 Fugle `tickers`（或富邦 snapshot）的權威上市清單，重建 `df_base_data.json` + 重新 seed Notion，一次解決「清單過期 + 下市殘留 + 只留上市」。（twstock 離線庫抓不出已下市的，不可靠。）

4. **上市/上櫃分流**
   目前 1287 支已幾乎全是上市；若要嚴格只抓上市，用權威清單過濾即可。

5. **上雲（GCP）做 24/7**
   筆電不適合長跑（睡眠/闔蓋會中斷）。專案已有 [utils/gcp_config.py](utils/gcp_config.py) 走 Secret Manager 的雛形。

---

## 回來後最常踩的坑（排查起點）

1. **跑不起來 / 啟動報錯** → 多半是 `.env` 有欄位是空的，或金鑰/data source ID 失效。照 [restart_checklist.md](restart_checklist.md) Step 1–6 逐項驗證。
2. **Notion 報 `object_not_found`** → `config.py` 裡的 data source ID，或 `users.json` 裡的 VIP `DB_ID` 失效／Integration 沒被加進該資料庫。
3. **主資料庫每支都「找不到頁面」** → 資料庫是空的，先跑 [seed_main_db.py](seed_main_db.py) 種入。
4. **大量 `429` / `FugleAPIError: Rate limit exceeded`** → 抓太快超過 60/min。確認 `FETCH_INTERVAL_SECONDS` 沒被調太小、`STOCK_API_KEYS` 的金鑰來自夠多不同帳號。
5. **大量 `404` / `查無此標的`** → df_base_data.json 裡有已下市的代碼，屬正常會被跳過；要清乾淨就用權威清單重建。
6. **新聞沒更新** → Selenium / Chrome 問題，或 Google Finance 改版導致選擇器失效（看 `news_scraper.py`）。
7. **log 看不到上一次的內容** → 正常，log 每次啟動都會覆寫。
