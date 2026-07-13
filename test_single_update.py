import os
import random
import time
import logging
from typing import List, Dict, Optional
# 由於我們無法直接導入 notion_client，這裡假設您已經用 pip install notion-client 安裝好了
from notion_client import Client
# --- 模擬導入必要的模組 ---
# 假設您的 config.py 和 .env 檔案都在正確的位置
from dotenv import load_dotenv
load_dotenv()

# 1. 取得目前這個腳本檔案 (test_single_update.py) 所在的資料夾的絕對路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
# 2. 將這個路徑和 .env 檔名組合起來，得到 .env 檔案的絕對路徑
dotenv_path = os.path.join(script_dir, '.env')
# 3. 在載入前，先檢查一下檔案是否存在，方便除錯
if os.path.exists(dotenv_path):
    print(f"成功找到 .env 檔案，路徑為: {dotenv_path}")
    # 4. 明確地告訴 load_dotenv()要去哪個路徑載入檔案
    load_dotenv(dotenv_path=dotenv_path)
else:
    print(f"錯誤：在預期路徑中找不到 .env 檔案: {dotenv_path}")
# --- 修正結束 ---

# 我們需要手動複製一些 config 變數過來進行測試
# MAIN_DATABASE_ID 是寫死在 config.py 的 data source id（非機密），直接從 config 取用，不從 .env 讀
from utils import config as project_config

class MockConfig:

    NOTION_API_KEY_LIST = os.getenv("NOTION_API_KEY_LIST", "").split(',')
    RESERVE_NOTION_API_KEY_LIST = os.getenv("RESERVE_NOTION_API_KEY_LIST", "").split(',')
    MAIN_DATABASE_ID = project_config.MAIN_DATABASE_ID

config = MockConfig()



# --- 設定一個簡單的日誌記錄器，讓所有訊息都顯示在螢幕上 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# --- 這是我們要測試的函式 (直接從您的程式碼中複製過來) ---
def update_notion_main_db(
    client: Client,
    data_source_id: str,
    stock_code: str,
    price: float,
    color: str,
    price_change_percent: str,
    failure_list: List[Dict],
    is_start: Optional[bool] = None,
    is_close: Optional[bool] = None
) -> bool:
    """
    【增強版】在指定的 Notion 資料庫中，尋找對應的股票代碼頁面並更新其價格。
    包含最多 3 次的 API 呼叫重試邏輯，第 3 次將使用備用 API Key。
    """
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 0.5
    RATE_LIMIT_DELAY = 3
    page_id = None # 在外面先定義 page_id

    # --- 步驟一：查詢頁面 (帶有重試機制) ---
    logger.info(f"--- 開始【步驟一：查詢】股票 {stock_code} ---")
    results = None
    client_to_use = client

    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                logger.info(f"正在進行第 {attempt + 1}/{MAX_RETRIES} 次查詢嘗試...")
            
            # ... (重試邏輯保持不變) ...
            if attempt == MAX_RETRIES - 1:
                if config.RESERVE_NOTION_API_KEY_LIST:
                    logger.info(f"查詢 {stock_code} 前兩次失敗，隨機選取備用 Notion Key...")
                    random_key = random.choice(config.RESERVE_NOTION_API_KEY_LIST)
                    client_to_use = Client(auth=random_key)
                else:
                    logger.warning(f"查詢 {stock_code} 已達最後嘗試次數，但未設定備用 Key。")

            query_filter = {
                "and": [
                    {"property": "股票代碼", "rich_text": {"equals": stock_code}},
                    {"property": "標記", "multi_select": {"contains": "所有股票"}}
                ]
            }
            query_resp = client_to_use.data_sources.query(
                data_source_id=data_source_id,
                filter=query_filter
            )
            results = query_resp.get("results", [])
            logger.info(f"查詢成功！API 回應中 'results' 的長度為: {len(results)}")
            break

        except Exception as e:
            # 加上 hasattr 檢查，避免在非 APIResponseError 時出錯
            if hasattr(e, 'status') and e.status == 429:
                logger.warning(f"[{stock_code}] Notion API 限流，等待 {RATE_LIMIT_DELAY} 秒")
                time.sleep(RATE_LIMIT_DELAY)
            else:
                logger.warning(f"查詢 Notion 頁面 {stock_code} 失敗 (第 {attempt + 1}/{MAX_RETRIES} 次): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY_SECONDS)

    if results is None:
        logger.error(f"❌ 查詢 Notion 頁面 {stock_code} 在重試 {MAX_RETRIES} 次後仍然徹底失敗。")
        failure_list.append({"type": "Notion Main DB Query", "stock_code": stock_code, "reason": "API 請求徹底失敗"})
        return False

    if not results:
        logger.warning(f"在 Notion 中找不到股票代碼為 '{stock_code}' 且標記為 '所有股票' 的頁面。")
        return False
    
    page_id = results[0]["id"]
    logger.info(f"成功找到頁面！Page ID 為: {page_id}")

    # --- 步驟二：準備更新內容並執行 ---
    logger.info(f"--- 開始【步驟二：更新】頁面 {page_id} ---")
    properties_to_update = {
        "即時價格": {"rich_text": [{"type": "text", "text": {"content": str(price)}, "annotations": {"bold": not is_start, "color": color.lower().strip()}}]},
        "漲跌幅": {"rich_text": [{"type": "text", "text": {"content": price_change_percent}, "annotations": {"bold": not is_start, "color": color.lower().strip()}}]}
    }
    
    client_to_use = client # 重置回預設的 client
    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                logger.info(f"正在進行第 {attempt + 1}/{MAX_RETRIES} 次更新嘗試...")

            # ... (重試邏輯保持不變) ...
            if attempt == MAX_RETRIES - 1:
                if config.RESERVE_NOTION_API_KEY_LIST:
                    logger.info(f"更新 {stock_code} 前兩次失敗，隨機選取備用 Key...")
                    random_key = random.choice(config.RESERVE_NOTION_API_KEY_LIST)
                    client_to_use = Client(auth=random_key)
                else:
                    logger.warning(f"更新 {stock_code} 已達最後嘗試次數，但未設定備用 Key。")

            client_to_use.pages.update(page_id=page_id, properties=properties_to_update)
            
            logger.info(f"✔✔✔ 成功更新 Notion 頁面 {stock_code} 價格為 {price} (顏色: {color})。")
            return True

        except Exception as e:
            logger.warning(f"更新 Notion 頁面 {stock_code} 失敗 (第 {attempt + 1}/{MAX_RETRIES} 次): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error(f"❌ 更新 Notion 頁面 {stock_code} 在重試 {MAX_RETRIES} 次後仍然徹底失敗。")
    failure_list.append({"type": "Notion Main DB Update", "stock_code": stock_code, "page_id": page_id, "reason": "API 更新請求徹底失敗"})
    return False

# --- 測試主程式 ---
def run_single_test():
    """執行一次單元測試"""
    logger.info("=============== 開始單元測試 ===============")

    ### --- 1. 請在此修改您要測試的資料 --- ###
    TEST_STOCK_CODE = "2330"  # 測試台積電
    TEST_PRICE = 100.99
    TEST_COLOR = "purple"
    TEST_PERCENT = "+10.00%"
    ### ------------------------------------ ###

    # 檢查設定是否載入成功
    if not config.NOTION_API_KEY_LIST or not config.MAIN_DATABASE_ID:
        logger.error("錯誤：請確保 .env 已設定 NOTION_API_KEY_LIST，且 config.py 已設定 MAIN_DATABASE_ID")
        return

    # 使用您的第一個 Notion API Key 來建立客戶端
    main_api_key = config.NOTION_API_KEY_LIST[0]
    notion_client = Client(auth=main_api_key)

    # 準備一個空的失敗列表
    failures = []

    # 執行我們要測試的函式
    success = update_notion_main_db(
        client=notion_client,
        data_source_id=config.MAIN_DATABASE_ID,
        stock_code=TEST_STOCK_CODE,
        price=TEST_PRICE,
        color=TEST_COLOR,
        price_change_percent=TEST_PERCENT,
        failure_list=failures,
        is_start=False
    )

    # --- 顯示測試結果 ---
    logger.info("=============== 測試結果 ===============")
    if success:
        logger.info("✅ 測試成功！函式回傳 True。")
    else:
        logger.error("❌ 測試失敗！函式回傳 False。")
    
    logger.info(f"失敗列表中的內容: {failures}")
    logger.info("========================================")


if __name__ == "__main__":
    run_single_test()