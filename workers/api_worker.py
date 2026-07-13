# workers/api_worker.py
"""API_Worker 類別的職責: 它的核心職責是管理和調度一個工作流程。
「接收任務清單 -> 區分 VIP -> 按照順序處理 -> 比較價格 -> 打包結果 -> 放入佇列」。它是一個「流程管理者」。
"""

import random
import time
import threading
import queue
import logging
import pandas as pd
from typing import List, Dict, Any,Tuple
# from utils.helpers import fetch_real_time_price # 從輔助函式檔導入
from fugle_marketdata import RestClient
import utils.config as config
from utils.task_builder import build_task_packet
# RESERVE_STOCK_API_KEY
logger = logging.getLogger(__name__)

# 節流：每個 worker 每抓一支股票之間的間隔(秒)。
# Fugle 免費版限流為「每帳號 60 次/分鐘」，每個 worker 各用一把(=一帳號)金鑰，
# 故設 ~1.1 秒/次 ≈ 54 次/分，安全壓在 60 以下。
# (未來改用富邦 snapshot 一次抓全市場後，此節流即可移除)
FETCH_INTERVAL_SECONDS = 1.1

class API_Worker(threading.Thread):
    def __init__(self, name: str,api_key: str,q: queue.Queue,
                failure_queue: queue.Queue, 
                vip_stocks: List[str], normal_stocks: List[str], 
                base_data: Dict[str, float], vip_database_dataframe_dist: Dict[str, pd.DataFrame],vvip_list: List[str]):
        super().__init__()
        self.name = name
        self.api_key = api_key
        self.q = q
        self.failure_queue =failure_queue
        self.vip_stocks = vip_stocks
        self.normal_stocks = normal_stocks
        self.base_data = base_data
        self.vip_database_dataframe_dist =vip_database_dataframe_dist
        self.vvip_list = vvip_list
        self.stock_client = RestClient(api_key=self.api_key,timeout=15)
        # 每個工人都有一個自己專屬的列表，用來記錄過程中失敗的股票代碼
        self.failed_stocks_list = []



    def run(self):
        logger.info(f"  [API Worker {self.name}] 啟動，VIP任務: {len(self.vip_stocks)}個, 普通任務: {len(self.normal_stocks)}個")
        logger.debug(f"  [API Worker {self.name}] VIP任務:{self.vip_stocks}")
        logger.debug(f"  [API Worker {self.name}] 一般任務:{self.normal_stocks}")
        logger.info("-"*20)
        # 1. 優先處理 VIP 股票
        for stock_code in self.vip_stocks:
            self._process_stock(stock_code,is_vip=True)
            time.sleep(FETCH_INTERVAL_SECONDS)  # 節流，壓在 60/min 以下

        # 2. 再處理普通股票
        for stock_code in self.normal_stocks:
            self._process_stock(stock_code, is_vip=False)
            time.sleep(FETCH_INTERVAL_SECONDS)  # 節流，壓在 60/min 以下

        # ⭐ 步驟 4: 工人完成所有任務後，回報自己的失敗清單
        if self.failed_stocks_list:
            self.failure_queue.put(self.failed_stocks_list)
            logger.info(f"  [API Worker {self.name}] 回報 {len(self.failed_stocks_list)} 筆失敗項目。")

        logger.info(f"  [API Worker {self.name}] 所有任務處理完畢。")



    # --- 用API去抓即時股價 ---
    # --- 因為現在非交易時間，我先用random的
    def _fetch_price(self,stock_code: str, base_price: float) -> Tuple[float, float]:
        """
        【增強版】嘗試呼叫 API 最多 3 次來獲取價格。
        前兩次使用預設 API Key，若皆失敗，第三次將隨機選用備用 Key。
        """
        MAX_RETRIES = 3
        RETRY_DELAY_SECONDS = 0.2
        
        # 預設使用工人自身的 stock_client
        client_to_use = self.stock_client
        for attempt in range(MAX_RETRIES):
            try:
                # ⭐ 關鍵 1: 檢查是否為倒數第二次
                if attempt == MAX_RETRIES - 2: # attempt 會是 0, 1, 2，所以 1 是倒數第二次
                    # 檢查是否有提供備用 Keys
                    if config.RESERVE_STOCK_API_KEY:
                        random_key = random.choice(config.RESERVE_STOCK_API_KEY)
                        # 從備用清單中隨機挑選一個 Key
                        client_to_use = RestClient(api_key=random_key)
                        # 【新增】只取前6個字元，專門用於日誌記錄，避免完整金鑰外洩
                        log_display_key = random_key[:6]    
                        logger.info(f"[{self.name}] {stock_code} 倒數第二次，選取RESERVE_STOCK_API_KEY (開頭為 {log_display_key}...) 進行嘗試。")
                    else:
                        logger.warning(f"[{self.name}] {stock_code} 未設定備用 API Key。")
                elif attempt == MAX_RETRIES - 1:#最後一次機會了
                    # 檢查是否有提供備用 Keys
                    if config.LATEST_CHANCE_STOCK_API_KEYS:
                        random_key = random.choice(config.LATEST_CHANCE_STOCK_API_KEYS)
                        # 從備用清單中隨機挑選一個 Key
                        client_to_use = RestClient(api_key=random_key)
                        # 【新增】只取前6個字元，專門用於日誌記錄，避免完整金鑰外洩
                        log_display_key = random_key[:6]    
                        logger.info(f"[{self.name}] {stock_code} 前兩次嘗試失敗，選取LATEST_CHANCE_STOCK_API_KEYS (開頭為 {log_display_key}...) 進行最後嘗試。")
                    else:
                        logger.warning(f"[{self.name}] {stock_code} 已達最後嘗試次數，但未設定 LATEST_CHANCE_STOCK_API_KEYS。")        
                    
                # ⭐ 步驟 2: 執行 API 請求
                response = client_to_use.stock.intraday.quote(symbol=str(stock_code))
                
                # ⭐ 步驟 3: 檢查 API 是否回傳了伺服器錯誤
                # Fugle API 在出錯時，通常會回傳一個包含 'statusCode' key值
                if response.get('statusCode'):
                    error_message = response.get('statusCode')
                    raise ValueError(f"API回傳伺服器端錯誤: {error_message}")
                # ⭐ 步驟 4: 解析價格
                price = response.get('lastPrice') 
                changePercent = response.get('changePercent')
                
                if price is not None and changePercent is not None:
                    # ✅ 成功情況：有價格，直接回傳
                    logger.debug(f"  [API Worker {self.name}] 查詢股價 {stock_code} 成功: 即時價格為:{price}")
                    return float(price), float(changePercent)
                else:
                    # 🟡 無資料情況：API 呼叫成功，但沒有成交價。這是「正常」的，不需重試
                    logger.warning(f" [API Worker {self.name}] 查詢股價 {stock_code} API 回應成功，但無即時價格資料 (可能無成交)，將沿用舊價格。")
                    return base_price, 0
                
            except ValueError as e:
                # 🔴 失敗情況：發生網路錯誤或 API 回傳了錯誤狀態，需要重試
                logger.warning(f"查詢股價 {stock_code} 失敗 (第 {attempt + 1}/{MAX_RETRIES} 次): {e}")
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"在 {RETRY_DELAY_SECONDS} 秒後重試...")
                    time.sleep(RETRY_DELAY_SECONDS)
                else:
                    logger.error(f"❌[API Worker {self.name}] 查詢股價 {stock_code} 在所有 {MAX_RETRIES} 次嘗試後均失敗。")
                    self.failed_stocks_list.append(stock_code)
                    return base_price, 0.0
            except Exception as e:
            # 區分永久性錯誤(404 查無標的) 與 暫時性錯誤(429 限流 / 網路層例外)
                status = getattr(e, "status_code", None)
                if status == 404:
                    # 永久性：此代碼在 Fugle 不存在(多半已下市)，重試或換金鑰都沒用，直接跳過
                    logger.warning(f"[{self.name}] {stock_code} 查無此標的 (404)，可能已下市，跳過不重試。")
                    self.failed_stocks_list.append(stock_code)
                    return base_price, 0.0
                # 暫時性(429 限流 / 網路層例外)：退避後重試（含換備用金鑰）
                logger.warning(f"查詢股價 {stock_code} 發生非預期錯誤 (第 {attempt + 1}/{MAX_RETRIES} 次): {type(e).__name__}: {e}")
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"在 {RETRY_DELAY_SECONDS} 秒後重試...")
                    time.sleep(RETRY_DELAY_SECONDS)
                else:
                    logger.error(f"❌[API Worker {self.name}] 查詢股價 {stock_code} 在所有 {MAX_RETRIES} 次嘗試後均失敗（非預期錯誤）: {type(e).__name__}: {e}")
                    self.failed_stocks_list.append(stock_code)
                    return base_price, 0.0



    def _process_stock(self, stock_code: str, is_vip: bool):
        try:
            base_price = float(self.base_data.get(stock_code, 0.0))
            current_price, change_percent_raw = self._fetch_price(stock_code, base_price)

            # 價格 → 紅綠/漲跌幅 → VIP 到價判斷 → 封包（共用邏輯，Fugle/富邦兩條路共用）
            task_packet = build_task_packet(
                stock_code=stock_code,
                current_price=current_price,
                base_price=base_price,
                change_percent_raw=change_percent_raw,
                is_vip=is_vip,
                vip_database_dataframe_dist=self.vip_database_dataframe_dist,
                vvip_list=self.vvip_list,
            )
            # 價格無變化時 build_task_packet 回傳 None → 不更新、不丟佇列
            if task_packet is not None:
                logger.debug(f"[API Worker {self.name}] task_packet {task_packet}")
                self.q.put(task_packet)
        except Exception as e:
            logger.error(f"[API Worker {self.name}] 處理 {stock_code} 時發生錯誤: {e}")
