import os
import time
import logging
import pprint
from typing import List, Dict, Optional

# --- 載入 .env 檔案的設定 ---
from dotenv import load_dotenv

script_dir = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(script_dir, '.env')
if os.path.exists(dotenv_path):
    print(f"成功找到 .env 檔案，路徑為: {dotenv_path}")
    load_dotenv(dotenv_path=dotenv_path)
else:
    print(f"警告：在預期路徑中找不到 .env 檔案: {dotenv_path}")

# --- 模擬導入必要的模組和函式 ---
# 我們需要從您的專案中，直接導入這些真實的函式和類別
from utils import config
from utils.helpers import update_notion_main_db, update_notion_watchlist, get_stock_name
from utils.mail_sender import EmailSender
from notion_client import Client

# --- 設定一個詳細的日誌記錄器 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 這是我們要測試的核心邏輯 (直接從您的 Notion_update_worker 中複製過來) ---
def process_task_for_test(
    task_packet: List, 
    notion_client: Client, 
    mailer: EmailSender,
    failed_notion_updates_list: List,
    failed_emails_list: List
):
    """
    模擬 Notion_update_worker 中的 _process_task 函式，用於單元測試。
    """
    logger.info(f"--- 開始處理新任務包 ---")
    logger.info(f"收到的任務包內容: {task_packet}")
    
    try:
        stock_code, price, color, price_change_percent = task_packet[0], task_packet[1], task_packet[2], task_packet[3]
        
        logger.info(f"--- 步驟 1: 準備更新主資料庫 (Stock: {stock_code}) ---")
        update_notion_main_db(
            client=notion_client,
            data_source_id=config.MAIN_DATABASE_ID,
            stock_code=stock_code, 
            price=price, 
            color=color,
            price_change_percent=price_change_percent,
            failure_list=failed_notion_updates_list
        )

        # 如果是 VIP 任務包
        if len(task_packet) == 5:
            logger.info(f"--- 步驟 2: 偵測到 VIP 任務，準備更新 Watchlist ---")
            user_details_list = task_packet[4]
            for user_detail in user_details_list:
                order_id = user_detail['order_id']
                
                logger.info(f"正在處理用戶 {order_id} 的 Watchlist 更新...")
                update_notion_watchlist(
                    client=notion_client,
                    order_id=user_detail['order_id'],
                    page_title=user_detail['page_title'],
                    status=user_detail['status'],
                    failure_list=failed_notion_updates_list
                )
                
                # 檢查並寄送郵件 (這部分我們先註解掉，專注於 Notion 更新)
                # if user_detail.get('email_needed'):
                #     logger.info(f"偵測到需要為用戶 {order_id} 寄送郵件...")
                #     # ... 寄信邏輯 ...

        logger.info(f"--- 任務包處理完畢 ---")

    except Exception as e:
        logger.error(f"處理任務包時發生未預期的嚴重錯誤: {e}", exc_info=True)

# --- 測試主程式 ---
def run_all_tests():
    logger.info("=============== 開始 Notion Worker 單元測試 ===============")

    # 檢查設定
    if not config.NOTION_API_KEY_LIST or not config.MAIN_DATABASE_ID:
        logger.error("錯誤：請確保您的 .env 檔案中已設定 NOTION_API_KEY_LIST 和 MAIN_DATABASE_ID")
        return

    # --- 準備測試環境 ---
    # 建立一個模擬的 Notion Client 和 Mailer
    main_api_key = config.NOTION_API_KEY_LIST[0]
    notion_client = Client(auth=main_api_key)
    # 暫時不需要真的寄信，所以 mailer 可以是 None 或一個模擬物件
    mailer = None 
    
    # 準備兩個空的失敗列表，用來接收函式回報的錯誤
    failed_notion = []
    failed_email = []

    # --- 定義您的兩個測試案例 ---
    test_case_1 = ['1732', 28.15, 'RED', '-0.88%']
    test_case_2 = ['2371', 35.8, 'GREEN', '-0.56%', [{'order_id': 'AC124', 'page_title': '2', 'target_high': 2000.0, 'target_low': 1325.0, 'note': '', 'status': '股價低於目標價_低'}]]

    # --- 執行測試 ---
    logger.info("\n\n===============【測試案例 1：普通任務】===============")
    process_task_for_test(test_case_1, notion_client, mailer, failed_notion, failed_email)

    logger.info("\n\n===============【測試案例 2：VIP 任務】===============")
    process_task_for_test(test_case_2, notion_client, mailer, failed_notion, failed_email)

    # --- 顯示最終測試結果 ---
    logger.info("\n\n=============== 所有測試執行完畢 ===============")
    if failed_notion:
        logger.error(f"❌ 測試過程中，發生了 {len(failed_notion)} 筆 Notion API 操作失敗：")
        pprint.pprint(failed_notion)
    else:
        logger.info("✅ 所有 Notion API 操作測試均未回報失敗。")
        
    # (如果未來啟用郵件測試，可以取消這段註解)
    # if failed_email:
    #     logger.error(f"❌ 測試過程中，發生了 {len(failed_email)} 筆郵件寄送失敗：")
    #     pprint.pprint(failed_email)
    # else:
    #     logger.info("✅ 所有郵件寄送測試均未回報失敗。")
    logger.info("==============================================")


if __name__ == "__main__":
    run_all_tests()