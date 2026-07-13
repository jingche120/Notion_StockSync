# Windows 排程部署指南（每個平日 08:45 自動執行）

目標：把服務部署到一台 **24 小時開機**的 Windows 電腦，讓「Windows 工作排程器（Task Scheduler）」每週一～五 08:45 自動執行 `main.py`。程式本身跑完一個交易日（初始化 → 盤中 → 盤後結算）會自動結束，因此排程器只需每天早上拉起一次，不需常駐服務。

```
工作排程器（週一~五 08:45 觸發）
    └─> scripts\run_stocksync.bat
          └─> 切到專案根目錄，用 .venv 的 python 執行 main.py
                ├─ 08:45~09:00 初始化（清大盤、重置顏色，實測約 4 分鐘）
                ├─ PRE_MARKET 等到 09:00
                ├─ IN_MARKET 每 5 分鐘循環到 13:30
                └─ POST_MARKET 富邦快照更新基準價 → 程式自動結束
```

---

## 步驟 1：環境建置（一次性）

照 [WINDOWS_SETUP.md](WINDOWS_SETUP.md) 的 **A～G 段**走完。重點回顧：

- [ ] 手動補上不進版控的檔案：`.env`、`information\client.pfx`、`df_base_data.json`
- [ ] 手動建立 `logs\` 空資料夾（缺了程式一啟動就崩潰）
- [ ] 建 venv 並安裝套件（`requirements.txt` 先註解 `fubon_neo` 那行，再單獨裝 **Windows 版** wheel：`fubon_neo-2.2.8-cp3x-win_amd64.whl`，macOS 版裝不起來）

## 步驟 2：檢查 `.env`（正式設定，兩個常見雷）

- [ ] **移除或註解 `MARKET_OPEN_TIME` / `MARKET_CLOSE_TIME`**
  這兩個是測試用的時段覆寫。留著的話，正式跑會用錯交易時間（例如還停在 22:25–22:40 的測試值）。移除後程式使用預設 09:00–13:30。
- [ ] `SENDER_EMAIL` / `SENDER_APP_PASSWORD` 使用 **Gmail** 憑證（Gmail 地址 + Google 應用程式密碼 16 碼）。SMTP 伺服器預設 `smtp.gmail.com:465`，不需額外設定。
- [ ] `PRICE_SOURCE=fubon`，且 `FUBON_CERT_PATH` 指向實際存在的 `.pfx` 檔（建議寫絕對路徑）。
- [ ] 若有 `SIMULATE_PRICE_CHANGE` 之類的測試旗標，確認已關閉。

## 步驟 3：先手動跑一次，確認能動

```powershell
cd C:\你的路徑\Notion_StockSync
scripts\run_stocksync.bat
```

打開 `logs\info.log` 確認：

- [ ] 出現「=============== 系統啟動 ===============」
- [ ] 出現「[富邦] 登入成功」
- [ ] **沒有**出現「提示：偵測到 .env 設定，已啟用測試市場時間」（有的話回步驟 2）

手動這關不過，排程一定也不會過。先修好再往下。

## 步驟 4：註冊排程

以**系統管理員身分**開 PowerShell，路徑改成你實際的專案位置：

```powershell
schtasks /Create /TN "NotionStockSync" /TR "C:\你的路徑\Notion_StockSync\scripts\run_stocksync.bat" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:45
```

成功會顯示「已順利建立排程工作 "NotionStockSync"」。

## 步驟 5：工作排程器 GUI 微調

開始選單搜尋「**工作排程器**」→ 工作排程器程式庫 → 找到 `NotionStockSync` → 右鍵「內容」：

| 頁籤 | 設定 | 原因 |
|---|---|---|
| 設定 | ✅ 勾「**若錯過排定的開始時間，盡快執行工作**」 | 防止當機/重開機/更新導致 08:45 沒跑就整天漏掉 |
| 條件 | ❌ 取消「只有在電腦使用 AC 電源時才啟動」 | 桌機無所謂；筆電必取消，否則沒插電就不跑 |
| 一般 | 維持「只有使用者登入時才執行」即可 | 24 小時開機且保持登入的話最單純；若想登出也能跑，改選「不論使用者登入與否」並輸入 Windows 密碼 |

## 步驟 6：立即測試排程（不等明天）

```powershell
schtasks /Run /TN "NotionStockSync"
```

然後看 `logs\info.log` 有沒有新的「系統啟動」。

## 步驟 7：隔天早上正式驗收

- [ ] 08:45 過後：`logs\info.log` 出現當天的「系統啟動」與「[富邦] 登入成功」
- [ ] 盤中（09:00 後）：Notion 主資料庫顏色/漲跌幅有更新、大盤指數頁面有新增
- [ ] 13:30 過後：log 出現「盤後: 使用富邦快照一次抓取全市場收盤價」；`df_base_data.json` 的修改時間是當天
- [ ] VVIP 驗證：自選股到價時收到 Gmail 警示信，且同狀態下一輪不重寄

---

## 日常維運

| 需求 | 指令 / 做法 |
|---|---|
| 查排程狀態與上次執行結果 | `schtasks /Query /TN "NotionStockSync" /V /FO LIST`（看「上次執行結果」，`0` 為成功） |
| 立刻手動跑一次 | `schtasks /Run /TN "NotionStockSync"` |
| 暫停排程（連假前等） | `schtasks /Change /TN "NotionStockSync" /DISABLE`，恢復用 `/ENABLE` |
| 改觸發時間 | `schtasks /Change /TN "NotionStockSync" /ST 08:30` |
| 刪除排程 | `schtasks /Delete /TN "NotionStockSync" /F` |
| 更新程式碼 | 專案目錄 `git pull`；若 `requirements.txt` 有變動，記得在 venv 內重新安裝 |

## 已知限制與故障排除

- **國定假日照跑**：排程器只認週一～五，台股休市日（春節、228…）程式仍會啟動，跑一輪靜態價後自行結束，無害。想省這一輪就用上表的 `/DISABLE` 手動暫停。
- **排程沒跑**：先查 `schtasks /Query ... /V` 的「上次執行結果」。常見原因：路徑含中文/空格但 `/TR` 沒加引號、電腦當時在關機/休眠、筆電沒插電且條件頁沒取消 AC 限制。
- **排程有跑但程式失敗**：看 `logs\info.log` / `logs\debug.log`（注意：每次執行**覆寫**，只保留最近一次）。常見：憑證路徑錯（富邦登入失敗 cert file location）、`.env` 缺變數、venv 套件不齊。
- **時區**：排程器用的是 Windows 系統時區，確認電腦時區為台北 (UTC+8)，否則 08:45 會對錯時間。
