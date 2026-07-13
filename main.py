# main.py
from dotenv import load_dotenv
load_dotenv() # 在所有程式碼之前，最優先載入 .env 檔案

import time
import json
import queue
import datetime
import logging
from typing import List, Dict, Any,Optional
import requests
import pandas as pd
import pprint
from notion_client import Client, APIResponseError
import threading
import random
# --- 從我們拆分好的模組中導入所有需要的東西 ---
# from news_scraper import scraper_news_and_index  # 暫時停用新聞爬蟲（不依賴 Selenium/Chrome）
from utils import config
from utils.helpers import split_list_into_n_chunks_numpy,fetch_relation_page_worker,fetch_all_notion_db_pages,for_each_vip_to_fetch_notion_data,update_notion_main_db,update_notion_watchlist,get_stock_name,notion_api_for_vip
from utils.helpers import get_price_safely
# from utils.helpers import split_list_into_n_chunks_numpy,update_notion_main_db,for_each_vip_to_fetch_notion_data,notion_api_for_vip,fetch_relation_page_worker
from workers.api_worker import API_Worker
from workers.notion_worker import Notion_update_worker
from utils.mail_sender import EmailSender
from fugle_marketdata import RestClient # 確保導入 RestClient

# --- 系統日誌設定(修改成有一個info以上和debug以上的各一個) ---

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
log_format = '%(asctime)s - %(levelname)s - %(message)s'
formatter = logging.Formatter(log_format)


# 3. 建立並設定 INFO 檔案的處理器 (Handler)
#    這個 handler 只處理 INFO 等級以上的訊息
info_handler = logging.FileHandler(f"{config.LOG_PATH}/info.log", mode='w', encoding='utf-8')
info_handler.setLevel(logging.INFO)
info_handler.setFormatter(formatter)

# 4. 建立並設定 DEBUG 檔案的處理器 (Handler)
#    這個 handler 處理所有 DEBUG 等級以上的訊息
debug_handler = logging.FileHandler(f"{config.LOG_PATH}/debug.log", mode='w', encoding='utf-8')
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(formatter)

# 5. 將這兩個處理器都加到根記錄器中
logger.addHandler(info_handler)
logger.addHandler(debug_handler)

# (建議) 為了避免 Notion client 等第三方函式庫也印出大量 DEBUG log，可以將它們的 logger 等級設高一點
logging.getLogger('notion_client').setLevel(logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)

# ------

# 在 logger 設定完成後，檢查是否啟用了測試市場時間，並印出提示
if config.IS_TEST_MARKET_TIME:
    logger.info("=" * 60)
    logger.info(f"提示：偵測到 .env 設定，已啟用測試市場時間 ({config.TEST_MARKET_TIME_INFO})")
    logger.info("=" * 60)


def initialization_worker(worker_id: int, api_key: str, chunk: list, base_data: dict):
    """
    這是一位工人的工作流程。
    他會用自己專屬的 API Key，處理分配給他的那一塊任務 (chunk)。
    """
    logger.info(f"[初始化工人 {worker_id}] 啟動，負責 {len(chunk)} 筆資料。")
    # 每一位工人都建立自己獨立的 Notion Client
    client = Client(auth=api_key)
    
    # 1. 為這位工人建立一個專屬的失敗清單
    initialization_failures = []
    for stock_code in chunk:
        update_notion_main_db(
            client=client,
            data_source_id=config.MAIN_DATABASE_ID,
            stock_code=stock_code, 
            price=base_data.get(stock_code, 0.0),
            color="default",
            price_change_percent="--",
            failure_list=initialization_failures,
            is_start=True
        )


# 新聞爬蟲與股價更新解耦：股價每 1 分鐘一輪，但新聞最多每 NEWS_SCRAPE_INTERVAL_SECONDS
# 秒才跑一次。用 module 層級變數記住上次爬取時間，跨輪保存。None 代表還沒爬過。
_last_news_scrape_time: Optional[float] = None


def get_current_state() -> str:
    """判斷當前市場狀態：盤前、盤中、盤後"""
    now = datetime.datetime.now().time()
    if now < config.MARKET_OPEN_TIME:
        return "PRE_MARKET"
    elif config.MARKET_OPEN_TIME <= now <= config.MARKET_CLOSE_TIME:
        return "IN_MARKET"
    else:
        return "POST_MARKET"

def produce_with_fugle(task_q, master_vip_list, master_normal_list, base_data,
                       vip_database_dataframe_dist, api_notion_failure_q):
    """抓價來源＝Fugle：建立 N 個 API_Worker（各用一把金鑰）逐支抓價，
    把封包丟進 task_q，並等所有 worker 完成。"""
    num_api_workers = len(config.STOCK_API_KEY)
    api_workers_list = []
    for i in range(num_api_workers):
        vip_chunk = master_vip_list[i::num_api_workers]
        normal_chunk = master_normal_list[i::num_api_workers]
        worker = API_Worker(
            name=f"Worker-{i+1}",
            api_key=config.STOCK_API_KEY[i],
            q=task_q,
            failure_queue=api_notion_failure_q,
            vip_stocks=vip_chunk,
            normal_stocks=normal_chunk,
            base_data=base_data,
            vip_database_dataframe_dist=vip_database_dataframe_dist,
            vvip_list=config.VVIP_ORDERS,
        )
        api_workers_list.append(worker)
        worker.start()
    for worker in api_workers_list:
        worker.join()
    logger.info("[Info] 所有 API Worker 已完成數據抓取。")


def run_core_loop(base_data: Dict[str, float], all_stock_codes: List[str],mailer: EmailSender,api_notion_failure_q: queue.Queue,email_failure_q: queue.Queue,is_close: Optional[bool] = False):
    """執行一個完整的核心循環（週期由 config.CORE_LOOP_DURATION_SECONDS 決定）。"""
    loop_start_time = time.time()
    logger.info(f"\n--- {datetime.datetime.now()} | 開始新一輪核心循環 ---")

    # --- 任務一：抓取新聞（與 1 分鐘股價更新解耦，最多每 5 分鐘跑一次）---
    global _last_news_scrape_time
    now_ts = time.time()
    need_news = (
        _last_news_scrape_time is None
        or (now_ts - _last_news_scrape_time) >= config.NEWS_SCRAPE_INTERVAL_SECONDS
    )
    # === 新聞爬蟲暫時停用：整段略過，不啟動 Selenium/Chrome ===
    logger.info("[Step 1] 新聞爬蟲已暫時停用，本輪略過新聞。")
    # if need_news:
    #     try:
    #         logger.info("[Step 1] 啟動新聞爬蟲 更新即時新聞...")
    #         scraper_news_and_index()
    #         _last_news_scrape_time = time.time()
    #         logger.info(f"更新即時新聞完成")
    #
    #     except Exception as e:
    #         logger.error(f"❌ 抓取新聞時發生錯誤: {e}", exc_info=True)
    #         # 新版本
    # else:
    #     wait_left = config.NEWS_SCRAPE_INTERVAL_SECONDS - (now_ts - _last_news_scrape_time)
    #     logger.info(f"[Step 1] 距上次爬新聞未滿 {config.NEWS_SCRAPE_INTERVAL_SECONDS} 秒（還差 {wait_left:.0f} 秒），本輪略過新聞。")
        
    # --- 任務二：抓取 VIP 資料 ---
    unique_stock_codes_set = set() # 預設為空集合
    vip_database_dataframe_dist = {} # 預設為空字典    

    try:
        # === VIP/VVIP 啟用中 ===
        # 抓各用戶自選股，組出 unique_stock_codes_set / vip_database_dataframe_dist，
        # 下游據此產生 master_vip_list，封包帶第 5 元素 user_details_list，
        # VIP watchlist 狀態更新照常執行；VVIP 到價（且狀態改變）時額外寄 email。
        logger.info("[Step 2] 抓取 VIP/VVIP 自選股資料...")
        unique_stock_codes_set, vip_database_dataframe_dist = notion_api_for_vip(config.USER_CONFIGS)

        logger.info(f"存取 VIP Watchlists 完成，共找到 {len(unique_stock_codes_set)} 支獨特 VIP 股票。")
        logger.debug(f"所有 VIP 股票列表: {unique_stock_codes_set}")
        # --- 開始排版 Log ---
        if vip_database_dataframe_dist:
        # 1. 準備一個列表，先放入開頭的訊息
            formatted_logs = ["存取完成，VIP 用戶資料詳細如下："]
        
             # 2. 遍歷字典中的每一個項目 (key 是訂單號, value 是對應的 DataFrame)
            for order_id, df in vip_database_dataframe_dist.items():
        
                 # 3. 為每個用戶建立一個獨立、格式化的文字區塊
                 #    - 使用分隔線讓不同用戶的資料更清晰
                 #    - 使用 .to_string() 可以確保 DataFrame 內容被完整印出，不會被截斷
                log_block = (
                    f"\n========================================\n  訂單號 (Order ID): {order_id}\n{df.to_string()}----------------------------------------\n"
                )
                # 4. 將這個格式化好的文字區塊，加到我們的列表中
                formatted_logs.append(log_block)
             # 5. 最後，用「換行符號」將列表中的所有文字全部組合起來，再一次性送給 logger
            final_log_message = "\n".join(formatted_logs)
            logger.debug(f"VIP 用戶資料詳細內容：\n{final_log_message}")

    except Exception as e:
        logger.error(f"❌ 存取 VIP Watchlists 時發生嚴重錯誤: {e}", exc_info=True)
        # 如果 VIP 資料都抓不到，後續的 API 請求就沒有意義了，可以直接跳過這一輪
        time.sleep(config.CORE_LOOP_DURATION_SECONDS) # 發生錯誤時也休息一下
        return

    # --- 後續的資源配置與工人啟動... ---
    # 即使新聞抓取失敗，程式仍然可以帶著 VIP 資料繼續執行到這裡
    logger.info("[Step 3] 正在配置資源與準備任務...") 
    task_q = queue.Queue()
    master_vip_list = [code for code in all_stock_codes if code in unique_stock_codes_set]
    master_normal_list = [code for code in all_stock_codes if code not in unique_stock_codes_set]
    logger.info(f"任務分配：VIP 列表共 {len(master_vip_list)} 支，普通列表共 {len(master_normal_list)} 支。")
    logger.debug(f"Master VIP List: {master_vip_list}")
    logger.debug(f"Master Normal List: {master_normal_list}")


    # Step 3 & 4. 建立並啟動工人
    # [Step 4] 先啟動 Notion 消費者（阻塞式 q.get()，會等到有封包或結束信號才動作，先啟動很安全）
    logger.info("[Step 4] 建立並啟動所有 Notion 工人（消費者）...")
    notion_workers_list = []
    num_notion_workers = len(config.NOTION_API_KEY_LIST)
    for i in range(num_notion_workers):
        worker = Notion_update_worker(
            name=f"Notion-Worker-{i+1}",
            api_key=config.NOTION_API_KEY_LIST[i],
            q=task_q,
            notion_failure_queue=api_notion_failure_q,
            mailer=mailer,
            email_failure_queue=email_failure_q,
        )
        notion_workers_list.append(worker)
        worker.start()

    # [Step 5] 依 PRICE_SOURCE 選用抓價來源（生產者），把股價封包灌進 task_q
    logger.info(f"[Step 5] 抓價來源：{config.PRICE_SOURCE}，開始生產股價封包...")
    if config.PRICE_SOURCE == "fubon":
        from workers.fubon_producer import produce_with_fubon  # 延遲匯入：沒裝 fubon_neo 的環境不受影響
        produce_with_fubon(task_q, master_vip_list, master_normal_list, base_data,
                           vip_database_dataframe_dist, api_notion_failure_q)
    else:
        produce_with_fugle(task_q, master_vip_list, master_normal_list, base_data,
                           vip_database_dataframe_dist, api_notion_failure_q)

    # 生產完成 → 對每個 Notion Worker 放一個 None 當結束信號
    for _ in range(num_notion_workers):
        task_q.put(None)

    # 等待所有 Notion Worker 處理完佇列並結束
    for worker in notion_workers_list:
        worker.join()
    logger.info("[Info] 所有 Notion Worker 已處理完畢。")
    # <-- 3. 處理Notion更新失敗的 -->
    if not api_notion_failure_q.empty():
        failed_tasks  = []
        while not api_notion_failure_q.empty():
            failed_list_from_worker = api_notion_failure_q.get() 
            # 將列表中的項目逐一加入到總列表中
            failed_tasks.extend(failed_list_from_worker)
        formatted_failures = pprint.pformat(failed_tasks, indent=4)
        logger.error(f"⚠️ 循環中，API/Notion 操作失敗 {len(failed_tasks)} 筆，詳細內容如下：\n{formatted_failures}")

        
    # 處理 Email 的失敗 (您之前的程式碼漏掉了這個檢查)
    if not email_failure_q.empty():
        failed_emails = []
        while not email_failure_q.empty():
            failed_list_from_worker = email_failure_q.get()
            failed_emails.extend(failed_list_from_worker)
            
        formatted_failures = pprint.pformat(failed_emails, indent=4)
        logger.error(f"⚠️ 循環中，Email 寄送失敗 {len(failed_emails)} 筆，詳細內容如下：\n{formatted_failures}")        

    if not task_q.empty():
        logger.warning(f"警告：佇列中仍有 {task_q.qsize()} 個未處理的任務。")

    # 計算剩餘等待時間
    elapsed = time.time() - loop_start_time
    sleep_time = config.CORE_LOOP_DURATION_SECONDS - elapsed
    if sleep_time > 0:
        logger.info(f"循環耗時 {elapsed:.2f} 秒，等待 {sleep_time:.2f} 秒。")
        time.sleep(sleep_time)
    else:
        logging.warning(f"循環處理超時: {abs(elapsed):.2f} 秒")

def run_post_market_tasks_worker(worker_id: int, api_key: str,chunk: list,base_data: dict, result_queue: queue.Queue):
    #   呼叫 Fugle API 取得該股票的closePrice
    logger.info(f"[初始化工人 {worker_id}]呼叫 Fugle API 取得該股票的closePrice啟動，負責 {len(chunk)} 筆資料。")
    local_price_map = {}
        
    for stock_code in chunk:
        # 直接呼叫我們新的共用工具函式
        response = get_price_safely(
            worker_name=f"盤後工人 {worker_id}",
            stock_code=stock_code,
            api_key=api_key
        )        
        

        # --- 解析結果 ---
        if response:
            price = response.get('closePrice')
            if price is None:
                logger.warning(f"API 回應中無 'closePrice'，正在嘗試備用的 'lastPrice' for {stock_code}...")
                price = response.get('lastPrice')

            if price is not None:
                local_price_map[stock_code] = float(price)
            else:
                logger.error(f"❌ API 回應中同時缺少 'closePrice' 和 'lastPrice'，stock_code {stock_code} 將沿用舊價。")
                local_price_map[stock_code] = base_data.get(stock_code, 0.0)
        else:
            # 如果 fetch_quote_with_retries 回傳 None，代表徹底失敗
            local_price_map[stock_code] = base_data.get(stock_code, 0.0)

    if local_price_map:
        result_queue.put(local_price_map)
    logger.info(f"[盤後工人 {worker_id}] 已完成所有任務並回報結果。")        
        


def run_post_market_tasks(
    base_data: Dict[str, float], 
    all_stock_codes: List[str], 
    mailer: EmailSender
):
    """
    【選項A版本】執行所有盤後任務。
    1. 執行最後一次核心循環以處理通知。
    2. 使用即時 API 重新抓取所有股票的最後價格。
    3. 將這些價格批次寫回基準檔案。
    """
    logger.info("--- 進入盤後結算流程 (方案 A：使用即時 API) ---")
    # ✅ 1. 為盤後任務建立【所有】需要的失敗佇列
    api_notion_failures_post_market = queue.Queue()
    email_failures_post_market = queue.Queue()
    # --- 任務一：執行最後一次的核心循環，確保所有通知都已處理 ---
    logger.info("[盤後任務 Step 1/3] 執行最後一次價格檢查與 VIP 通知...")
    try:
        run_core_loop(base_data, all_stock_codes, mailer, api_notion_failure_q=api_notion_failures_post_market,email_failure_q=email_failures_post_market,is_close=True)
        logger.info("✔ 最後一次核心循環執行完畢。")
    except Exception as e:
        logger.error(f"❌ 執行最後一次核心循環時發生錯誤: {e}", exc_info=True)

    # ✅ 3. 在核心循環結束後，立刻處理這一輪的失敗回報
    # 處理 API 和 Notion 的失敗
    if not api_notion_failures_post_market.empty():
        failed_tasks = []
        # 從佇列中取出所有工人回報的失敗列表
        while not api_notion_failures_post_market.empty():
            # .get() 會取出工人回報的整個失敗列表 (a list of dicts)
            failed_list_from_worker = api_notion_failures_post_market.get() 
            # 將列表中的項目逐一加入到總列表中
            failed_tasks.extend(failed_list_from_worker)
        
        # logger.error(f"⚠️ 在【盤後】核心循環中，API/Notion 操作失敗 {len(failed_tasks)} 筆：")
        # 使用 pprint 讓字典或複雜列表的輸出更易讀
        formatted_failures = pprint.pformat(failed_tasks, indent=4)

        # 2. 將摘要和格式化後的詳細內容，一起交給 logger.error 記錄
        #    我們在摘要和詳細內容之間加上換行符 \n，讓 log 更清晰
        logger.error(
            f"⚠️ 在【盤後】核心循環中，API/Notion 操作失敗 {len(failed_tasks)} 筆，詳細內容如下：\n{formatted_failures}"
        )


    # 處理 Email 的失敗
    if not email_failures_post_market.empty():
        failed_tasks = []
        while not email_failures_post_market.empty():
            failed_list_from_worker = email_failures_post_market.get()
            failed_tasks.extend(failed_list_from_worker)
            
        formatted_failures = pprint.pformat(failed_tasks, indent=4)

        # 2. 將摘要和格式化後的詳細內容，一起交給 logger.error 記錄
        #    我們在摘要和詳細內容之間加上換行符 \n，讓 log 更清晰
        logger.error(
            f"⚠️ 在【盤後】處理 Email 操作失敗 {len(failed_tasks)} 筆，詳細內容如下：\n{formatted_failures}"
        )

    logger.info("-" * 20)

    # --- 任務二：全面抓取所有股票的最終價格 ---
    logger.info("[盤後任務 Step 2/3] 開始全面抓取收盤價，準備更新基準檔案...")

    # 這就是您提到的 prepare_for_post 字典，我們稱之為 final_price_map
    final_price_map = {}

    # 優先：富邦快照（1 次呼叫抓全市場，無 rate limit）
    if config.PRICE_SOURCE == "fubon":
        try:
            from workers.fubon_producer import fetch_close_price_map  # 延遲匯入，與核心循環同模式
            logger.info("盤後: 使用富邦快照一次抓取全市場收盤價...")
            final_price_map = fetch_close_price_map(all_stock_codes, base_data)
        except Exception as e:
            logger.error(f"❌ 富邦快照抓收盤價時發生錯誤: {e}", exc_info=True)
            final_price_map = {}
        if not final_price_map:
            logger.warning("⚠️ 富邦快照整批失敗，自動退回 fugle 舊路抓收盤價...")

    # fugle 路：PRICE_SOURCE=fugle 走這裡；富邦整批失敗也自動退到這裡
    if not final_price_map:
        result_queue = queue.Queue()
        chunks = split_list_into_n_chunks_numpy(all_stock_codes,len(config.STOCK_API_KEY))
        threads = []
        logger.info("盤後: 使用多工人去富果抓收盤價")

        for i, chunk in enumerate(chunks):
            if i < len(config.STOCK_API_KEY):
                api_key = config.STOCK_API_KEY[i]
                thread = threading.Thread(
                    target=run_post_market_tasks_worker,
                    args=(i + 1, config.STOCK_API_KEY[i], chunk,base_data,result_queue)
                )
                threads.append(thread)
                thread.start() # 啟動工人！讓他開始工作
                logger.info(f"初始化_工人 {i+1} 已派出，任務列表 (共 {len(chunk)} 項)。")

        # 4. 等待所有工人完成工作
        for thread in threads:
            thread.join()

        # ⭐ 關鍵 3: 從佇列中取出所有工人的成果，並合併到主字典中
        logger.info("所有盤後工人已收工，開始合併結果...")
        while not result_queue.empty():
            worker_result = result_queue.get()
            logger.debug(f"從佇列合併了 {len(worker_result)} 筆來自工人的價格資料。")
            final_price_map.update(worker_result)

    logger.info(f"✔ 已成功蒐集 {len(final_price_map)} 筆股票的最終價格。")
    # --- 任務三：使用蒐集好的字典，一次性更新基準檔案 ---
    logger.info("[盤後任務 Step 3/3] 開始將最終價格寫回基準檔案...")
    try:
        # 步驟 1: 【關鍵修正】在寫入前，永遠從檔案中重新讀取一次最新的基準資料。
        # 這樣才能確保我們是在最新的狀態上進行更新，而不是使用程式啟動時的舊資料。
        with open(config.BASE_DATA_FILE, 'r', encoding='utf-8') as f:
            current_base_data = json.load(f)

        # 步驟 2: 在記憶體中將最新的價格更新到副本中
        # 用final_price_map去更新current_base_data
        current_base_data.update(final_price_map)
        logger.debug(f"[Test] 準備寫入檔案的 current_base_data: {current_base_data}")
        logger.debug("="*20)
        logger.debug(f"[Test] 本次盤後蒐集到的 final_price_map: {final_price_map}")

        # 步驟 3: 使用 'w' (寫入) 模式，將更新後的完整資料一次性覆蓋寫回。
        with open(config.BASE_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_base_data, f, indent=4, ensure_ascii=False)
            
        logger.info(f"✔ 盤後結算完成！基準股價檔案 '{config.BASE_DATA_FILE}' 已成功更新。")
    except Exception as e:
        logger.error(f"❌ 更新基準檔案時發生未知錯誤: {e}", exc_info=True)







if __name__ == "__main__":
    logger.info("=============== 系統啟動 ===============")
    try:
        with open(config.BASE_DATA_FILE, 'r', encoding='utf-8') as f:
            base_data = json.load(f)
        all_stock_codes = list(base_data.keys())
        logger.info(f"成功從 {config.BASE_DATA_FILE} 載入 {len(all_stock_codes)} 筆股票代碼。")
        logger.debug(f"載入的完整股票代碼列表: {all_stock_codes}")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.error(f"基準檔案 {config.BASE_DATA_FILE} 不存在或格式錯誤，將從盤後任務開始。")
        # 重新載入一次
        with open(config.BASE_DATA_FILE, 'r', encoding='utf-8') as f:
            base_data = json.load(f)
        # base_data 會繼續存在在程式
        all_stock_codes = list(base_data.keys())

    # 【優化】在主程式啟動時，只建立一次 Mailer 物件
    email_sender  = EmailSender(
        sender_email=config.SENDER_EMAIL,
        app_password=config.SENDER_APP_PASSWORD,
        smtp_server=config.SMTP_SERVER,
        smtp_port=config.SMTP_PORT
    )
    # 初始化
    #step 1先把昨天的大盤指數刪掉
    
    # 最後把大盤的都刪掉
    client = Client(auth=config.NOTION_API_KEY_LIST[0])
    all_page_ids = []
    has_more = True
    next_cursor = None
    
    logger.info("把昨天大盤的資料都刪掉")
    logger.info("步驟一：正在取得資料庫中所有頁面的 ID...")
    # 透過迴圈取得所有分頁的資料
    while has_more:
        response = client.data_sources.query(
            data_source_id=config.TPE_INDEX_ID,
            start_cursor=next_cursor
        )
        # 從回應中取得頁面ID
        page_ids = [page['id'] for page in response.get("results", [])]
        all_page_ids.extend(page_ids)
        
        has_more = response.get("has_more", False)
        next_cursor = response.get("next_cursor")

    logger.info(f"查詢完成，共找到 {len(all_page_ids)} 個頁面。")

    # 逐一封存所有頁面
    logger.info("\n步驟二：開始逐一刪除 (封存) 所有頁面...")
    for i, page_id in enumerate(all_page_ids):
        try:
            client.pages.update(page_id=page_id, archived=True)
            logger.debug(f"({i + 1}/{len(all_page_ids)}) 已成功刪除頁面: {page_id}")
            # 加入延遲以避免觸發 API 速率限制 (每秒約 3 次請求)
            time.sleep(0.35) 
        except Exception as e:
            logger.error(f"刪除頁面 {page_id} 時發生錯誤: {e}")
    logger.info("\n所有頁面均已成功移至垃圾桶。")
    
    #step 2 將即時股價顏色改成bloack，漲跌幅' -- ' 
    chunks = split_list_into_n_chunks_numpy(all_stock_codes,len(config.NOTION_API_KEY_LIST))
    logger.info("初始化:將即時股價顏色改成bloack，漲跌幅''")
    threads = []
    for i, chunk in enumerate(chunks):
        # 確保我們有對應的 API Key
        if i < len(config.NOTION_API_KEY_LIST):
            api_key = config.NOTION_API_KEY_LIST[i]
            
            # 建立一個執行緒，告訴他要去哪個函式(target)，以及要帶什麼參數(args)
            thread = threading.Thread(
                target=initialization_worker, 
                args=(i + 1, api_key, chunk,base_data)
            )
            threads.append(thread)
            thread.start() # 啟動工人！讓他開始工作
            logger.info(f"初始化_工人 {i+1} 已派出，任務列表 (共 {len(chunk)} 項)。")

    # 4. 等待所有工人完成工作
    for thread in threads:
        thread.join() # 主程式會在這裡暫停，直到這個工人執行緒完成為止

    logger.info("✔ 所有初始化工人都已完成任務！")
    logger.info("===========初始化完成:將即時股價顏色改成bloack，漲跌幅''============")

    # 判斷狀態開始
    while get_current_state() == "PRE_MARKET":
        logger.debug(f"盤前等待中...下次檢查時間: {(datetime.datetime.now() + datetime.timedelta(seconds=60)).strftime('%H:%M:%S')}")
        time.sleep(60)

    # --- 盤中核心迴圈 ---
    logger.info("進入盤中監控流程...")
    while get_current_state() == "IN_MARKET":
        api_and_notion_failures = queue.Queue()
        email_failures   = queue.Queue()
        run_core_loop(base_data, all_stock_codes,email_sender,api_notion_failure_q=api_and_notion_failures,email_failure_q=email_failures )

        # 3. 核心函式執行完畢後，立刻檢查並處理這次循環中發生的所有失敗
        if not api_and_notion_failures.empty():
            failed_tasks = []
            while not api_and_notion_failures.empty(): # ✅ 修正：處理正確的佇列
                failed_list_from_worker = api_and_notion_failures.get()
                failed_tasks.extend(failed_list_from_worker)
                
                
            formatted_failures = pprint.pformat(failed_tasks, indent=4)

            # 2. 將摘要和格式化後的詳細內容，一起交給 logger.error 記錄
            #    我們在摘要和詳細內容之間加上換行符 \n，讓 log 更清晰
            logger.error(
                f"⚠️ 循環中，API/Notion 操作失敗 {len(failed_tasks)} 筆，詳細內容如下：\n{formatted_failures}"
            )            


        # 處理 Email 的失敗
        if not email_failures.empty(): # ✅ 新增一個獨立的 if 區塊
            failed_tasks = []
            while not email_failures.empty():
                failed_list_from_worker = email_failures.get()
                failed_tasks.extend(failed_list_from_worker)
            logger.error(f"⚠️ 循環中，Email 寄送失敗 {len(failed_tasks)} 筆：")
            # pprint(failed_tasks)
            
            
            formatted_failures = pprint.pformat(failed_tasks, indent=4)

            # 2. 將摘要和格式化後的詳細內容，一起交給 logger.error 記錄
            #    我們在摘要和詳細內容之間加上換行符 \n，讓 log 更清晰
            logger.error(
                f"⚠️ 循環中，Email 寄送失敗 {len(failed_tasks)} 筆，詳細內容如下：\n{formatted_failures}"
            )
            

    # --- 盤後結算任務 ---
    # 當上面的迴圈結束時，代表時間必定已進入盤後
    logger.info("狀態：市場已收盤，自動執行每日結算任務。")
    run_post_market_tasks(base_data, all_stock_codes, email_sender)
    
    logger.info("所有盤後任務執行完畢，系統將在 10 秒後關閉。")
    time.sleep(10)
    
    logger.info("=============== 系統正常關閉 ===============")