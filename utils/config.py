# utils/config.py
import datetime
import os
import json
import logging

from dotenv import load_dotenv

# 自給自足地載入 .env：即使有腳本/測試繞過 main.py 直接 import config，
# 也能讀到 FUBON_* 等環境變數。load_dotenv 冪等，main.py 已先載過也無妨。
load_dotenv()

LOG_PATH = "logs"

# 1. 先定義好正式的、預設的市場時間
DEFAULT_OPEN_TIME = datetime.time(9, 0)
DEFAULT_CLOSE_TIME = datetime.time(13, 30)

# 2. 嘗試從環境變數讀取測試用的時間
open_time_str = os.getenv("MARKET_OPEN_TIME")
close_time_str = os.getenv("MARKET_CLOSE_TIME")

# 3. 如果成功讀到 .env 中的時間字串，就解析它
# 3. 新增兩個變數，讓 main.py 可以讀取它們來印出日誌
IS_TEST_MARKET_TIME = False
TEST_MARKET_TIME_INFO = ""

# 4. 根據 .env 的設定，決定最終的市場時間，我的config中不應該有log，而是設一個flag讓main判斷是否成功
if open_time_str and close_time_str:
    try:
        MARKET_OPEN_TIME = datetime.datetime.strptime(open_time_str, "%H:%M").time()
        MARKET_CLOSE_TIME = datetime.datetime.strptime(close_time_str, "%H:%M").time()
        # 如果成功，就設定旗標和資訊字串
        IS_TEST_MARKET_TIME = True
        TEST_MARKET_TIME_INFO = f"{open_time_str} - {close_time_str}"
    except ValueError:
        # 格式錯誤時，使用預設值
        MARKET_OPEN_TIME = DEFAULT_OPEN_TIME
        MARKET_CLOSE_TIME = DEFAULT_CLOSE_TIME
else:
    # .env 中沒有設定，使用預設值
    MARKET_OPEN_TIME = DEFAULT_OPEN_TIME
    MARKET_CLOSE_TIME = DEFAULT_CLOSE_TIME


# --- Credentials & API Keys (loaded from environment variables) ---
NOTION_EMAIL = os.getenv("NOTION_EMAIL")
NOTION_PASSWORD = os.getenv("NOTION_PASSWORD")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_APP_PASSWORD = os.getenv("SENDER_APP_PASSWORD")

# 富果
stock_api_keys_str = os.getenv("STOCK_API_KEYS", "")
STOCK_API_KEY = stock_api_keys_str.split(',') if stock_api_keys_str else []

# 備用富果
reserve_stock_api_keys_str = os.getenv("RESERVE_STOCK_API_KEYS", "")
# RESERVE_STOCK_API_KEY = reserve_stock_api_keys_str if reserve_stock_api_keys_str else []
RESERVE_STOCK_API_KEY = reserve_stock_api_keys_str.split(',') if reserve_stock_api_keys_str else []
# 最後一次機會
latest_chancereserve_stock_api_keys_str = os.getenv("LATEST_CHANCE_STOCK_API_KEYS", "")
LATEST_CHANCE_STOCK_API_KEYS = latest_chancereserve_stock_api_keys_str.split(',') if latest_chancereserve_stock_api_keys_str else []

# --- 抓價來源 ---
# "fugle"：逐支 quote（舊路，受 60/min 限流，需節流/多帳號；保留為退路）
# "fubon"：富邦 snapshot 一次抓全上市（300/min，一輪一次呼叫，不漏股）
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "fugle")

# --- 富邦憑證（PRICE_SOURCE=fubon 時才需要）---
FUBON_ACCOUNT = os.getenv("FUBON_ACCOUNT")
FUBON_PASSWORD = os.getenv("FUBON_PASSWORD")
# 憑證 .pfx 路徑；預設指向 information/ 下的憑證檔（.pfx 已被 gitignore，不進版控）
FUBON_CERT_PATH = os.getenv("FUBON_CERT_PATH", "information/client.pfx")

# 富邦快照要查詢並「合併」成單一查表的市場別。
# 監控清單含上市(TSE)與創新板(TIB)，且為防未來加上櫃/興櫃，預設一次涵蓋四個板。
# 注意：創新板(如 8487 愛爾達-創)獨立於 TSE，只查 TSE 會漏掉。PSB(興櫃戰略新板)目前 0 檔，略過。
# 每輪呼叫次數 = 市場別數量，遠低於富邦 300/min。可用 .env FUBON_SNAPSHOT_MARKETS 覆寫（逗號分隔）。
_fubon_markets_str = os.getenv("FUBON_SNAPSHOT_MARKETS", "TSE,OTC,ESB,TIB")
FUBON_SNAPSHOT_MARKETS = [m.strip() for m in _fubon_markets_str.split(",") if m.strip()]

# --- 測試用：模擬價格變動 ---
# 盤後快照回傳的是收盤靜態價，每輪都一樣（去重會跳過全部）。
# 設為 true 時，富邦路會讓每檔 lastPrice 各有 0.5 機率 +1，模擬盤中跳動以便測試 1 分鐘更新與去重。
# 正式環境務必拿掉或設 false。（漲跌幅不重算，測試階段忽略其準確性）
SIMULATE_PRICE_CHANGE = os.getenv("SIMULATE_PRICE_CHANGE", "false").lower() == "true"

#  Notion_api
notion_api_keys_str = os.getenv("NOTION_API_KEY_LIST", "")
NOTION_API_KEY_LIST = notion_api_keys_str.split(',') if notion_api_keys_str else []

#  備用Notion_api
reserve_notion_api_keys_str = os.getenv("RESERVE_NOTION_API_KEY_LIST", "")
RESERVE_NOTION_API_KEY_LIST = reserve_notion_api_keys_str.split(',') if reserve_notion_api_keys_str else []


# --- 檔案路徑設定 (Non-sensitive) ---
BASE_DATA_FILE = 'df_base_data.json'
LOG_FILE = 'notion_marquee.log'
#* 這個 MAIN_DATABASE_ID 是直接寫入資料庫資料的ID，不是網址
MAIN_DATABASE_ID = "3846bce3-ed2c-8016-bdea-000bbc31c1fd"

# --- 效能與週期設定 (Non-sensitive) ---
# 富邦快照模式：核心循環改為 1 分鐘一輪（原 Fugle 受限流為 5 分鐘）。
CORE_LOOP_DURATION_SECONDS = 180   # 3 minute (as an integer)
QUEUE_CHECK_TIME_SECONDS = 285   # 4 minutes 45 seconds (as an integer)
# 新聞爬蟲與股價更新解耦：新聞最多每 NEWS_SCRAPE_INTERVAL_SECONDS 秒才跑一次。
NEWS_SCRAPE_INTERVAL_SECONDS = 300  # 5 minutes

# --- VIP List Data (Non-sensitive, but could be moved to a JSON file if it gets large) ---
# --- VIP List Data (從外部 JSON 檔案載入) ---
def load_user_configs():
    # 優先從環境變數 USER_CONFIGS_JSON 讀取（機密，放在 .env，不進版控）。
    # 內容為一段 JSON 陣列字串，格式同 users.example.json。
    env_json = os.getenv("USER_CONFIGS_JSON")
    if env_json:
        try:
            return json.loads(env_json)
        except json.JSONDecodeError:
            logger.error("錯誤：環境變數 USER_CONFIGS_JSON 不是合法的 JSON")
            return []

    # 退路：讀取本地 users.json（同樣不進版控，僅供本機開發）
    config_path = os.path.dirname(os.path.abspath(__file__))
    users_file_path = os.path.join(config_path, 'users.json')
    try:
        with open(users_file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"錯誤：找不到用戶設定，請設定 .env 的 USER_CONFIGS_JSON 或提供 {users_file_path}")
        return []
    except json.JSONDecodeError:
        logger.error(f"錯誤：用戶設定檔 {users_file_path} 格式錯誤")
        return []

# 載入用戶設定
USER_CONFIGS = load_user_configs()

# VVIP_ORDERS 仍然可以根據載入的 USER_CONFIGS 動態產生
VVIP_ORDERS = [
    user["order"] for user in USER_CONFIGS if user.get("VVIP") == "true"
]


# --- Block IDs (Non-sensitive) ---
NEWS_BLOCK_ID = [
    "219937079bf081479f6ddc7387e84154",
    "219937079bf081caa1e4d7e10025f84b",
    "219937079bf081ee8488f93198254799",
    "22e937079bf080a78d8fc9034b4cc80d",
    "219937079bf08166b85dca83e7181adb"
]
#* 這個ID是直接寫入data的
TPE_INDEX_ID = "3856bce3-ed2c-8025-8e1b-000b769fa22b"

