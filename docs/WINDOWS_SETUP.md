# Windows 設定步驟清單（fubon 抓價路）

把這個 macOS 開發的台股監控服務推上 git、clone 到 Windows 執行時，會踩到幾個跨平台與 git 的雷。
本清單針對 `PRICE_SOURCE=fubon` 抓價路，照順序做完即可在 Windows 上跑起來。**本專案不需更動程式碼，全部以設定／手動步驟解決。**

---

## 三個會直接讓你失敗的硬傷（先看這裡）

1. **富邦 SDK wheel 綁平台** — 專案內現有的 `fubon_neo-2.2.8-cp37-abi3-macosx_11_0_arm64.whl` 是 macOS ARM 版，Windows **裝不起來**；且 `requirements.txt` 第 48 行 `fubon_neo==2.2.8` 不在公開 PyPI，直接整包安裝會**失敗**。
2. **`logs/` 目錄缺失** — clone 後這個資料夾不存在，`main.py` 一啟動寫 log 就 `FileNotFoundError` **崩潰**。
3. **機密／資料檔被 gitignore** — `.env`、`.pfx`、`df_base_data.json`、`.whl` 都不會隨 push 帶過去，必須手動補。

下面依序處理。

---

## A. 在 macOS 這端 push 前要知道的事

`.gitignore` 會擋掉這些**執行必需但不入版控**的檔案，Windows 端 clone 後拿不到，須以安全管道（隨身碟／加密傳輸，**勿用 git**）手動帶過去：

| 檔案 | 為何不在 repo | 用途 |
|---|---|---|
| `.env` | 機密，gitignore | 所有金鑰／帳密 |
| `information/client.pfx` | `*.pfx` 被擋 | 富邦登入憑證 |
| `df_base_data.json` | 明確 gitignore | 前一日收盤價，`main.py` 會讀（main.py:412/423/439/447） |
| 富邦 SDK wheel | `*.whl` 被擋 | 且現有檔是 macOS 版，Windows 不能用（見 C） |

---

## B. Windows 端 clone 後要「手動補」的檔案

1. **`.env`** → 放到專案根目錄。確認下列都已設：
   - `PRICE_SOURCE=fubon`
   - `FUBON_ACCOUNT` / `FUBON_PASSWORD` / `FUBON_CERT_PATH`
   - `NOTION_API_KEY_LIST`
   - `SENDER_EMAIL` / `SENDER_APP_PASSWORD`
2. **`information\` 目錄** → git 不追空目錄，clone 後可能不存在。**先手動建立 `information` 資料夾**，再把 `client.pfx` 放進去（須與 `FUBON_CERT_PATH` 預設值 `information/client.pfx` 對應；或在 `.env` 改成你實際的路徑）。
3. **`df_base_data.json`** → 複製到專案根目錄。
4. **`logs\` 空資料夾** → 手動建立（關鍵，見 D）。

---

## C. 安裝 Python 環境（重點：富邦 wheel 綁平台）

1. 安裝與 macOS 同系列的 Python，建立並啟用 venv：
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   ```
2. **不要直接 `pip install -r requirements.txt`**（會卡在 `fubon_neo==2.2.8`）。正確做法：
   1. 暫時把 `requirements.txt` 中 `fubon_neo==2.2.8` 那行**註解或移除**，再安裝其餘套件：
      ```powershell
      python -m pip install --upgrade pip
      pip install -r requirements.txt
      ```
      （numpy 2.4.6 / pandas 3.0.3 在 Windows 有對應 wheel，需較新版 pip。）
   2. 到**富邦官方**下載 Windows 版 SDK wheel（檔名形如 `fubon_neo-2.2.8-cp3x-win_amd64.whl`），單獨安裝：
      ```powershell
      pip install fubon_neo-2.2.8-cp3x-win_amd64.whl
      ```

---

## D. `logs` 目錄缺失會直接崩潰（必處理）

- `utils/config.py:13` 定義 `LOG_PATH = "logs"`；`main.py:38,44` 用 `logging.FileHandler("logs/info.log" / "logs/debug.log")`。
- `*.log` 被 gitignore 且 git 不追空目錄 → clone 後 `logs/` 不存在 → `FileHandler` 丟 `FileNotFoundError`，程式一啟動就崩潰。
- **解法：clone 後務必先手動建立 `logs` 空資料夾再執行。**
  ```powershell
  mkdir logs
  ```
- 備註（未來可選優化，本次不做）：在 `main.py` 建立 handler 前加一行 `os.makedirs(config.LOG_PATH, exist_ok=True)` 即可一勞永逸。

---

## E. Selenium / Chrome（news_scraper.py）

- 需在 Windows 安裝 **Google Chrome 瀏覽器**。
- selenium 4.45 內建 Selenium Manager 會自動下載對應的 chromedriver，**不需**手動放 driver。
- 程式內 `--no-sandbox` / `--disable-dev-shm-usage`（news_scraper.py:25-26）是 Linux 取向，但在 Windows 無害，**不需更動**。

---

## F. 執行與其他注意

- **務必在專案根目錄**執行 `python main.py`：`FUBON_CERT_PATH`、`df_base_data.json`、`logs/` 都是相對 cwd 的相對路徑，在別的目錄啟動會找不到檔案。
- `.sh` 腳本（`git_push.sh` / `git_pull.sh` / `create_context.sh`）在 Windows cmd/PowerShell **不能直接跑**；改用原生 `git` 指令，或裝 Git Bash 執行。
- 路徑分隔符**不必改**：`main.py` 用的 `"logs/info.log"` 正斜線在 Windows 的 Python `open()` 可正常運作；`helpers.py` / `notion_worker.py` 已用 `pathlib`，跨平台 OK。

---

## G. 驗證（在 Windows 上）

1. 啟動前自查：`logs\`、`information\client.pfx`、`df_base_data.json`、`.env` 皆就位；
   ```powershell
   python -c "import fubon_neo"   # 不報錯代表 Windows wheel 裝對了
   ```
2. 用測試市場時間快速驗證：在 `.env` 設一個涵蓋當下時間的 `MARKET_OPEN_TIME` / `MARKET_CLOSE_TIME`，跑 `python main.py`。
3. 觀察 `logs\info.log` 是否出現「[富邦] 登入成功」與核心循環訊息；確認 Notion 主資料庫顏色／漲跌幅有更新、大盤指數頁面有新增。