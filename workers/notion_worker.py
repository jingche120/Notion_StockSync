# workers/notion_worker.py
import threading
import queue
import logging
from notion_client import Client, APIResponseError
import json
from pathlib import Path
from typing import Dict, List, Optional
import time
# 從輔助函式檔導入
from utils.helpers import update_notion_main_db, update_notion_watchlist,get_stock_name
from utils.mail_sender import EmailSender
from utils import config
logger = logging.getLogger(__name__)

class Notion_update_worker(threading.Thread):
    """數據消費者：從佇列取出任務包，解析並執行 Notion 更新。"""
    def __init__(self, name: str, api_key: str, q: queue.Queue,notion_failure_queue: queue.Queue ,mailer: EmailSender,email_failure_queue: queue.Queue):
        super().__init__()
        self.name = name
        self.api_key = api_key # 將傳入的 API Key 存起來
        self.q = q
        self.notion_failure_queue = notion_failure_queue 
        self.mailer =mailer
        self.email_failure_queue = email_failure_queue # 儲存失敗佇列
        # Client 物件被建立一次，並儲存為這個工人物件的一個屬性 (attribute)
        # 物件導向原則: __init__ (建構函式) 被建立時，就把它所有需要的、會長期持有的資源都準備好。
        self.client = Client(auth=self.api_key)
        self.stock_map = self._load_stock_map()
        # 每個工人都有自己專屬的列表，用來記錄過程中失敗的任務  
        self.failed_notion_updates_list = []
        self.failed_emails_list = []
        

# 用來讀取stock_code_to_name_map的，因為之後是用code去找name
    def _load_stock_map(self) -> dict:
        """從專案根目錄讀取股票代碼對照表"""
        try:
            # 1. 計算出根目錄的路徑
            # __file__ 是目前檔案 (notion_worker.py) 的路徑
            # .parent 是上一層 (workers 資料夾)
            # .parent.parent 就是上上一層 (專案根目錄)
            project_root = Path(__file__).parent.parent

            # 2. 組合出目標檔案的完整路徑
            # 假設您的檔案是 .json 格式
            map_file_path = project_root / "stock_code_to_name_map.json"

            # 3. 使用這個完整的路徑來讀取檔案
            with open(map_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 根據我們之前的討論，資料在 "code_to_name_map" 這個 key 裡面
            return data.get("code_to_name_map", {})

        except Exception as e:
            logger.error(f"讀取股票代碼對照表時發生錯誤: {e}")
            return {} # 如果失敗，回傳一個空字典

    def run(self):
        logger.info(f"  [{self.name}] 啟動，等待任務...")
        while True:
            task_packet = self.q.get()
            try:
                if task_packet is None: # 收到結束信號
                    logger.info(f"  [{self.name}] 收到結束信號，準備退出。")
                    break
                self._process_task(task_packet)
            finally:
                self.q.task_done()

        # ⭐ 步驟 3: 工人完成所有任務後，回報自己的更新Notion失敗清單
        if self.failed_notion_updates_list:
            self.notion_failure_queue.put(self.failed_notion_updates_list)
            logger.info(f"  [{self.name}] 回報 {len(self.failed_notion_updates_list)} 筆 Notion API 操作失敗項目。")
        # ⭐ 步驟 3: 工人完成所有任務後，回報自己的寄信失敗清單
        if self.failed_emails_list:
            self.email_failure_queue.put(self.failed_emails_list)
            logger.info(f"  [{self.name}] 回報 {len(self.failed_emails_list)} 筆寄信失敗項目。")
                
        logger.info(f"  [{self.name}]任務迴圈已結束。")

    def _process_task(self, task_packet,is_close: Optional[bool] = None):
        try:
            stock_code, price, color ,price_change_percent = task_packet[0], task_packet[1], task_packet[2], task_packet[3]
            update_notion_main_db(
                client=self.client,
                data_source_id=config.MAIN_DATABASE_ID,
                stock_code=stock_code, 
                price=price, 
                color=color,
                price_change_percent=price_change_percent, # 將漲跌幅傳進去
                failure_list=self.failed_notion_updates_list, # 傳入失敗列表
                is_close=is_close
            )

            # 如果是 VIP 任務包
            if len(task_packet) == 5:
                user_details_list = task_packet[4]
                logger.debug(f"[我所關心_1]VIP的user_details_list:{user_details_list}")
                for user_detail in user_details_list:
                    order_id = user_detail['order_id']
                    
                    user_config = next((u for u in config.USER_CONFIGS if u["order"] == order_id), None)
                    if not user_config:
                        logging.warning(f"在 config 中找不到訂單 {order_id} 的設定。")
                        continue
# def update_notion_watchlist(client: Client,api_key:str,order_id: str,page_title: str, status: str) -> bool:

                    # 更新個人 watchlist,不管是VIP還是VVIP都需要更新status
                    update_notion_watchlist(
                        client=self.client,
                        order_id=user_detail['order_id'],
                        page_title=user_detail['page_title'],
                        status=user_detail['status'],
                        failure_list=self.failed_notion_updates_list # 傳入失敗列表
                    )
                    logger.debug(f"[我所關心_2]VIP中要更改status狀態的update_notion_watchlist:{update_notion_watchlist}")
                    # 檢查並寄送郵件
                    if user_detail.get('email_needed'):
                        logger.debug(f"[我所關心_3]VIP中要寄信的user_details:{user_detail}")
                        # print(f"get_stock_name {get_stock_name('0050','stock_code_to_name_map')}")
                        alert_info = {
                            # "股票名稱": user_detail.get('stock_name') or get_stock_name(stock_code,"stock_code_to_name_map"),
                            # 用股票代碼去找出他公司名稱
                            "股票名稱": get_stock_name(stock_code,"stock_code_to_name_map"),
                            "股票代碼": stock_code,
                            "觸發狀態": user_detail.get('status', 'N/A'),
                            "目標價格_低": user_detail.get('target_low', 'N/A'),
                            "目標價格_高": user_detail.get('target_high', 'N/A'),
                            "即時價格": price
                        }
                        logger.debug(f"[]order_id:{order_id}即將寄送email，alert_info{alert_info}")

                        # 寄信
                        MAX_TRIES = 3
                        RETRY_DELAY_SECONDS = 3 # 每次重試前等待5秒
                        email_sent_successfully = False # 用一個旗標來追蹤最終狀態

                        for attempt in range(MAX_TRIES):
                            logger.debug(f"[{self.name}] 正在嘗試寄送郵件給 {user_config['email']} (股票: {stock_code}, 第 {attempt + 1}/{MAX_TRIES} 次)")                            # 呼叫您的寄信函式

                            success  = self.mailer.send_email(
                                recipient=user_config['email'],
                                alert_data=alert_info
                            )
                            if success:
                                email_sent_successfully = True
                                # 如果回傳 True，代表成功了
                                logger.debug("✔ 郵件寄送成功！")
                                break # 成功了，就用 break 跳出迴圈，不再重試
                            else:
                                if attempt < MAX_TRIES - 1: # 檢查是否還有重試機會
                                    logger.debug(f"在 {RETRY_DELAY_SECONDS} 秒後重試...")
                                    time.sleep(RETRY_DELAY_SECONDS)

                        # --- 迴圈結束後 ---
                        # 檢查最終的寄送狀態
                        if not email_sent_successfully:
                            # 如果迴圈跑完了，這個旗標仍然是 False，代表所有嘗試都失敗了
                            logger.error(f"❌ 在嘗試 {MAX_TRIES} 次後，郵件仍然無法寄送給 {user_config['email']}。")
                            # 程式會在這裡放棄寄信，繼續往下執行其他任務
                            self.failed_emails_list.append({
                                'recipient': user_config['email'],
                                'stock_code': stock_code,
                                'alert_data': alert_info
                            })
        except Exception as e:
            logger.error(f"[{self.name}] 處理任務包時發生錯誤: {e}, 任務包: {task_packet}", exc_info=True)
