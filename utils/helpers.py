# utils/helpers.py
import time
import random
import logging
import threading
from pprint import pprint
from notion_client import Client, APIResponseError
from typing import List, Dict, Any,Optional,Tuple, Set
from utils.mail_sender import EmailSender
from pathlib import Path 
from typing import Optional
import json
import numpy as np
from utils import config
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException
import pandas as pd
from fugle_marketdata import RestClient

# --- 模擬外部 API/爬蟲 的佔位函式 ---
logger = logging.getLogger(__name__)

# 自訂錯誤，方便 main.py 捕捉
class CrawlerLoginError(Exception):
    pass

# 初始化更新主DB
def split_list_into_n_chunks_numpy(all_stock_codes: List[Any], n: int) -> List[List[Any]]:
    """
    使用 NumPy 的 array_split 將一個列表盡量平均地切成 n 份。

    Args:
        data_list (List[Any]): 要被切分的原始列表。
        n (int): 想要切成的份數。

    Returns:
        List[List[Any]]: 一個包含了 n 個子列表的列表。
    """
    if n <= 0:
        logger.error(f"切分的份數 n (notion_api工人的數目)必須是正整數。")
        raise ValueError("切分的份數 n (notion_api工人的數目)必須是正整數。")
    if not all_stock_codes:
        # 注意：此函式為通用切分工具（初始化主DB、關聯查詢等多處共用），
        # 收到空列表不一定是 df_base_data.json 的問題，訊息保持中性以免誤導除錯方向。
        logger.warning(f"要切分的列表為空，回傳 {n} 個空清單。")

        return [[] for _ in range(n)] # 如果是空列表，回傳 n 個空列表

    # np.array_split 會自動處理無法整除的情況，非常方便
    split_arrays = np.array_split(all_stock_codes, n)
    # 將結果從 numpy array 轉回 python list
    return [arr.tolist() for arr in split_arrays]


def _rollup_first_plain_text(prop: Dict[str, Any], default: str) -> str:
    """從 rollup(array) 屬性中安全取出第一筆的 plain_text（支援 rich_text / title / number）。

    對應新版 VIP 自選股 schema：「想追蹤公司」「即時價格」「漲跌幅」等欄位
    是透過「股票名稱」relation 彙總（show_original）出來的 rollup，值已直接可用。
    """
    arr = prop.get("rollup", {}).get("array", [])
    if not arr:
        return default
    first = arr[0]
    inner_type = first.get("type")
    if inner_type == "number":
        num = first.get("number")
        return str(num) if num is not None else default
    text_list = first.get(inner_type, []) if inner_type else []
    if text_list and isinstance(text_list, list):
        return text_list[0].get("plain_text", default)
    return default


# 建立notion_api去找關聯DB的工人
def fetch_relation_page_worker(
    api_key: str, 
    page_ids_chunk: List[str], 
    results_dict: Dict[str, Any], 
    worker_id: int
):
    """
    這是一位工人的工作流程。
    他會用自己專屬的 API Key，處理分配給他的那一塊 page_id 列表。
    """
    logger.info(f"[關聯查詢工人 {worker_id}] 啟動，負責 {len(page_ids_chunk)} 筆頁面。")
    client = Client(auth=api_key) # 每個工人使用自己的 Client 物件，確保線程安全
    
    for page_id in page_ids_chunk:
        try:
            page_data = client.pages.retrieve(page_id=page_id)
            # 將查到的結果，放入大家共享的 results_dict 字典中
            # Python 的字典寫入是線程安全的，所以這裡不需要額外加鎖
            results_dict[page_id] = page_data.get("properties", {})
        except APIResponseError as e:
            logger.warning(f"[關聯查詢工人 {worker_id}] 抓取關聯頁面 {page_id} 失敗: {e}")
            results_dict[page_id] = {} # 即使失敗，也給一個空字典，避免後續出錯


def fetch_all_notion_db_pages(client: Client, data_source_id: str) -> List[Dict[str, Any]]:
    """
    【新增的輔助函式】從指定的 Notion 資料庫中，自動處理分頁並獲取所有頁面資料。
    """
    all_pages = []
    has_more = True
    next_cursor = None
    while has_more:
        try:
            response = client.data_sources.query(
                data_source_id=data_source_id,
                start_cursor=next_cursor
            )
            all_pages.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")
        except APIResponseError as e:
            logger.error(f"抓取 Notion 分頁時發生 API 錯誤: {e}")
            break
    return all_pages

def for_each_vip_to_fetch_notion_data(api_key: str, data_source_id: str) -> Optional[pd.DataFrame]:
    """
    【多線程升級版】使用 Notion API 高效抓取指定 VIP 用戶的所有追蹤資料。
    """
    client = Client(auth=api_key)
    all_pages = fetch_all_notion_db_pages(client, data_source_id)

    if not all_pages:
        return pd.DataFrame() # 如果沒抓到任何頁面，回傳一個空的 DataFrame

    all_rows_data = []
    header_list = ["編號", "想追蹤的公司", "股票代碼", "即時價格", "漲跌幅", "目標價_低", "目標價_高", "狀態", "是否通知", "備註"]
    
    # 建立一個列表，存放所有需要一次性查詢的關聯頁面 ID
    # 【schema 相容】「想追蹤公司」有兩種可能：
    #   - relation（舊 schema）：需要抓關聯頁面才能拿到股票代碼等資料
    #   - rollup（新 schema）：代碼已由 rollup 直接給出，不需要再查關聯頁面
    relation_ids_to_fetch = []
    for page in all_pages:
        follow_prop = page.get("properties", {}).get("想追蹤公司", {})
        if follow_prop.get("type") != "relation":
            continue  # rollup 新 schema：跳過，不需抓關聯頁
        try:
            relation_id = follow_prop["relation"][0]["id"]
            relation_ids_to_fetch.append(relation_id)
        except (KeyError, IndexError):
            continue

    # ⭐ 步驟二：【核心修改】將任務分配給多個工人同時執行
    relation_pages_data = {}  # 準備一個空的字典，讓所有工人把結果放進來
    threads = []
    if relation_ids_to_fetch:  # rollup schema 下為空，直接略過派工
        num_workers = 2 # 您指定使用 2 個線程
        # 將任務列表切成 2 份
        chunks = split_list_into_n_chunks_numpy(relation_ids_to_fetch, num_workers)

        for i, chunk in enumerate(chunks):
            if not chunk: continue # 如果某個塊是空的，就跳過

            # 建立一個執行緒 (工人)
            thread = threading.Thread(
                target=fetch_relation_page_worker, # 告訴工人要去哪個函式工作
                args=(api_key, chunk, relation_pages_data, i + 1) # 把需要的工具和任務交給他
            )
            threads.append(thread)
            thread.start() # 派出工人，讓他開始在背景工作

        # ⭐ 步驟三：等待所有工人完成工作
        for thread in threads:
            thread.join() # 主程式會在這裡暫停，直到這位工人完成他的所有工作
        logger.info(f"所有關聯查詢工人都已完成，共獲取 {len(relation_pages_data)} 筆詳細資料。")

    # 現在，我們帶著所有需要的資料，開始解析
    for page in all_pages:
        try:
            properties = page.get("properties", {})
            title_list = properties.get('編號', {}).get('title', [])
            # 2. 檢查列表是否為空，如果不為空，才去取第一個元素
            if title_list:
                # row_name在這裡取值了
                row_name = title_list[0].get('plain_text', '')
            else:
                logger.warning(f"在資料庫 '{data_source_id}' 中發現一列沒有填寫 '編號'，已跳過。")
                continue # continue 會立刻結束這次的迴圈，去處理下一筆資料            
            
            # 【優化】將所有欄位的取值方式都統一成更安全的形式            
            # 先取得 rich_text 列表，再安全地取出第一個元素
            target_low_list = properties.get('目標價_低', {}).get('rich_text', [])
            row_target_low = target_low_list[0].get('plain_text', '0') if target_low_list else '0'
            target_high_list = properties.get('目標價_高', {}).get('rich_text', [])
            row_target_high = target_high_list[0].get('plain_text', '0') if target_high_list else '0'            
            note_list = properties.get('備註', {}).get('rich_text', [])
            row_note = note_list[0].get('plain_text', '') if note_list else ''
            row_status = properties.get('狀態', {}).get('status', {}).get('name', 'N/A')
            row_alert = properties.get('是否通知', {}).get('select', {}).get('name', '否')
            
            # # --- 安全地取值 (使用 .get() 並提供預設值) ---
            # row_name = properties.get('編號', {}).get('title', [{}])[0].get('plain_text', '')
            # row_target_low = properties.get('目標價_低', {}).get('rich_text', [{}])[0].get('plain_text', '0')
            # row_target_high = properties.get('目標價_高', {}).get('rich_text', [{}])[0].get('plain_text', '0')
            # row_status = properties.get('狀態', {}).get('status', {}).get('name', 'N/A')
            # row_alert = properties.get('是否通知', {}).get('select', {}).get('name', '否')
            # # row_note = properties.get('備註', {}).get('rich_text', [{}])[0].get('plain_text', '')
            # if properties['備註']['rich_text'] != []:
            #     row_note = properties['備註']['rich_text'][0]['plain_text']
            # else:
            #     row_note = ""


            # --- 取得股票代碼等關聯資料（schema 相容：rollup 直讀 / relation 走關聯頁）---
            follow_prop = properties.get("想追蹤公司", {})
            follow_type = follow_prop.get("type")

            if follow_type == "rollup":
                # 新 schema：「想追蹤公司」是 rollup（經「股票名稱」relation 彙總出股票代碼），
                # 「即時價格」「漲跌幅」同為 rollup，值直接可用，不需再查關聯頁面。
                row_of_want_to_follow_company_code = _rollup_first_plain_text(follow_prop, 'N/A')
                if row_of_want_to_follow_company_code == 'N/A':
                    logger.warning(f"在資料庫 '{data_source_id}' 中發現一列 '{row_name}' 的『想追蹤公司』rollup 為空（尚未關聯股票），已跳過。")
                    continue
                # 公司名稱由代碼對照表反查（此欄僅供顯示，下游任務不使用）
                row_of_want_to_follow_company = get_stock_name(
                    row_of_want_to_follow_company_code, "stock_code_to_name_map") or 'N/A'
                row_of_want_to_follow_company_price = _rollup_first_plain_text(
                    properties.get('即時價格', {}), '0.0')
                row_of_want_to_follow_company_percent = _rollup_first_plain_text(
                    properties.get('漲跌幅', {}), '--')
            elif follow_prop.get("relation", []):
                # 舊 schema：relation，從預先抓好的關聯頁面資料中取值
                relation_list = follow_prop.get("relation", [])
                relation_id = relation_list[0].get("id")
                relation_props = relation_pages_data.get(relation_id, {})

                company_title_list = relation_props.get('股票名稱', {}).get('title', [])
                row_of_want_to_follow_company = company_title_list[0].get('plain_text', 'N/A') if company_title_list else 'N/A'

                code_list = relation_props.get('股票代碼', {}).get('rich_text', [])
                row_of_want_to_follow_company_code = code_list[0].get('plain_text', 'N/A') if code_list else 'N/A'

                price_list = relation_props.get('即時價格', {}).get('rich_text', [])
                row_of_want_to_follow_company_price = price_list[0].get('plain_text', '0.0') if price_list else '0.0'

                percent_list = relation_props.get('漲跌幅', {}).get('rich_text', [])
                row_of_want_to_follow_company_percent = percent_list[0].get('plain_text', '--') if percent_list else '--'
            else:
                # 如果使用者沒有關聯任何公司，就跳過這一列
                logger.warning(f"在資料庫 '{data_source_id}' 中發現一列 '{row_name}' 沒有關聯 '想追蹤的公司'，已跳過。")
                continue
            
            row_data = [
                row_name, row_of_want_to_follow_company, row_of_want_to_follow_company_code,
                row_of_want_to_follow_company_price, row_of_want_to_follow_company_percent,
                row_target_low, row_target_high, row_status, row_alert, row_note
            ]
            all_rows_data.append(row_data)

        except Exception as e:
            logger.error(f"解析 VIP DB 的某一列時出錯: {e} 高機率是使用者某個row的值怪怪的", exc_info=True)
            continue
            
    df = pd.DataFrame(data=all_rows_data, columns=header_list)
    logger.debug(f"訂單號 '{data_source_id}' 的資料為:\n{df}。")

    return df



def notion_api_for_vip(vip_list) -> Tuple[Set[str], Dict[str, pd.DataFrame]]:
    # 回傳會長這樣
    # ({"0050","0051"},{"AK001":一個dataframe,"AK002":一個dataframe)
    vip_database_dataframe_dist = {}
    for vip_info  in vip_list:
        order_id = vip_info["order"]
        order_db_id = vip_info["DB_ID"]
        a_user_df = for_each_vip_to_fetch_notion_data(config.NOTION_API_KEY_LIST[0],order_db_id)
        vip_database_dataframe_dist[order_id] = a_user_df
    
    unique_stock_codes_set = {
            code
            for df in vip_database_dataframe_dist.values()
            if '股票代碼' in df.columns
            for code in df['股票代碼'].dropna().unique()
            # .dropna() 清除空值    
        }
    return unique_stock_codes_set, vip_database_dataframe_dist

        
        
# def web_crawler_for_vip(email, password, vip_list) -> Tuple[Set[str], Dict[str, pd.DataFrame]]:

#         for vip_info  in vip_list:
#             order_id = vip_info["order"]
#             db_url = vip_info["DB_URL"]
#             # 呼叫爬蟲物件的抓取方法
#             user_df = crawler.scrape_database(db_url)
#             if user_df is not None and not user_df.empty:
#                 vip_database_dataframe_dist[order_id] = user_df
#                 logger.info(f"訂單號 '{order_id}' 的db_url為{db_url}且資料為{user_df}。")
#             else:
#                 logger.warning(f"訂單號 '{order_id}' 的資料抓取失敗或為空，已跳過。")

#         # 3. 處理最終結果
#         unique_stock_codes_set = {
#             code
#             for df in vip_database_dataframe_dist.values()
#             if '股票代碼' in df.columns
#             for code in df['股票代碼'].dropna().unique()
#             # .dropna() 清除空值    
#         }
#         return unique_stock_codes_set, vip_database_dataframe_dist
#     except CrawlerLoginError:
#         # 如果登入就失敗了，直接回傳空結果
#         self.mailer.send_email(
#         recipient=user_config['email'],
#         alert_data=alert_info
#         )
#         logger.error("爬蟲因登入失敗而終止。")
        
#         return set(), {}
#     except Exception as e:
#         logger.error(f"執行爬蟲主流程時發生嚴重錯誤: {e}", exc_info=True)
#         return set(), {}
#     finally:
#         # 4. 無論成功或失敗，都確保瀏覽器被關閉
#         crawler.close()



# def for_each_vip_to_fetch_notion_data(api_key: str, data_source_id: str) -> Optional[pd.DataFrame]:
#     client= Client(auth=api_key)
#     a_data = client.databases.query(database_id=a_database_id)
#     rows = a_data.get("results", [])
#     # pprint(f"rows:{rows}")
#     all_rows_data = []
#     header_list = ["編號","想追蹤的公司","股票代碼","即時價格","漲跌幅","目標價_低","目標價_高","狀態","是否通知","備註"]
#     for row in rows:
#         row_data=[]
#         properties = row.get("properties", {})
#         row_name = properties['編號']['title'][0]['plain_text']
#         row_target_low = properties['目標價_低']['rich_text'][0]['plain_text']
#         row_target_high = properties['目標價_高']['rich_text'][0]['plain_text']
#         row_status =  properties['狀態']['status']['name']
#         row_alert = properties['是否通知']['select']['name']
#         # row_of_note = properties['備註']['rich_text'][0]['plain_text']        
#         if properties['備註']['rich_text'] != []:
#             row_note = properties['備註']['rich_text'][0]['plain_text']
#         else:
#             row_note = ""
#         # 關聯(主DB)的部分
#         row_tilte = properties.get("編號")
#         result = properties.get("想追蹤公司")
#         result = result.get("relation")
#         relation_id = result[0].get("id")
#         # pprint(f"id:{id}")
#         relation_page = client.pages.retrieve(page_id = relation_id)
#         relation_page_properties = relation_page.get("properties")
#         row_of_want_to_follow_company = relation_page_properties['股票名稱']['title'][0]['plain_text']
#         row_of_want_to_follow_company_code = relation_page_properties['股票代碼']['rich_text'][0]['plain_text']
#         row_of_want_to_follow_company_price = relation_page_properties['即時價格']['rich_text'][0]['plain_text']
#         row_of_want_to_follow_company_percent = relation_page_properties['漲跌幅']['rich_text'][0]['plain_text']
        
#         # row_details = {
#         #     "編號": row_name,
#         #     "公司名稱": row_of_want_to_follow_company,
#         #     "股票代碼": row_of_want_to_follow_company_code,
#         #     "即時價格": row_of_want_to_follow_company_price,
#         #     "漲跌幅": row_of_want_to_follow_company_percent,
#         #     "目標價_低": row_target_low,
#         #     "目標價_高": row_target_high,
#         #     "狀態": row_status,
#         #     "是否通知": row_alert,
#         #     "備註": row_note
#         # }
#         # print("--- 單筆資料詳情 ---")
#         # pprint(row_details)
#         # pprint("="*20)
#         row_data= [
#             row_name,
#             row_of_want_to_follow_company,
#             row_of_want_to_follow_company_code,
#             row_of_want_to_follow_company_price,
#             row_of_want_to_follow_company_percent,
#             row_target_low,
#             row_target_high,
#             row_status,
#             row_alert,
#             row_note   
#         ]
#         all_rows_data.append(row_data)
    
#     df = pd.DataFrame(data=all_rows_data, columns=header_list)
#     return df


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
    RATE_LIMIT_DELAY =3
    # --- 步驟一：查詢頁面 (帶有重試機制) ---
    results = None
    client_to_use = client # 預設使用傳入的 client
    
    for attempt in range(MAX_RETRIES):
        try:
            # 最後一次嘗試時，切換到備用 Key
            if attempt == MAX_RETRIES - 1:
                if config.RESERVE_NOTION_API_KEY_LIST:
                    logger.info(f"查詢 {stock_code} 前兩次失敗，隨機選取備用 Notion Key 進行最後嘗試...")
                    random_key = random.choice(config.RESERVE_NOTION_API_KEY_LIST)
                    client_to_use = Client(auth=random_key) # 建立臨時 client
                else:
                    logger.warning(f"查詢 {stock_code} 前兩次失敗，但未設定備用 Notion Key。")

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
            break # 查詢成功，跳出重試迴圈

        except Exception as e:
            
            logger.warning(f"查詢 Notion 頁面 {stock_code} 失敗 (第 {attempt + 1}/{MAX_RETRIES} 次): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)    
        # 如果重試 3 次後仍然失敗 (results 依然是 None)，則直接返回
    if results is None:
        logger.error(f"❌ 查詢 Notion 頁面 {stock_code} 在重試 {MAX_RETRIES} 次後仍然徹底失敗。")
        # ⭐ 將失敗資訊加入列表
        failure_list.append({
            "type": "Notion Main DB Query",
            "stock_code": stock_code,
            "reason": "沒有找到符合的page"
        })
        return False

    if not results:
        logger.warning(f"在 Notion 中找不到股票代碼為 '{stock_code}' 且標記為 '所有股票' 的頁面。")
        return False
    page_id = results[0]["id"]    
    if len(results) > 1:
        logger.warning(f"找到多個股票代碼為 '{stock_code}' 的頁面，將只更新第一個。")
            # --- 步驟二：準備更新內容並執行 ---

    # 判斷是否是is_start
    # 不論是什麼情境，都準備好完整的更新內容
    properties_to_update = {
        "即時價格": {
            "rich_text": [{
                "type": "text",
                "text": {"content": str(price)},
                "annotations": {
                    # is_start 為 True 時，字體不加粗
                    "bold": not is_start, 
                    "color": color.lower().strip()
                }
            }]
        },
        "漲跌幅": {
            "rich_text": [{
                "type": "text",
                "text": {"content": price_change_percent},
                "annotations": {
                    "bold": not is_start,
                    "color": color.lower().strip()
                }
            }]
        }
    }
    client_to_use = client # 重置回預設的 client
    for attempt in range(MAX_RETRIES):
        try:
            # 同樣地，最後一次嘗試時切換到備用 Key
            if attempt == MAX_RETRIES - 1:
                if config.RESERVE_NOTION_API_KEY_LIST:
                    logger.info(f"更新 {stock_code} 前兩次失敗，隨機選取備用 Notion Key 進行最後嘗試...")
                    random_key = random.choice(config.RESERVE_NOTION_API_KEY_LIST)
                    client_to_use = Client(auth=random_key)
                else:
                    logger.warning(f"更新 {stock_code} 已達最後嘗試次數，但未設定備用 Notion Key。")

            client_to_use.pages.update(page_id=page_id, properties=properties_to_update)
            
            # 根據不同情境印出 Log
            if is_start:
                logger.debug(f"✔ 初始化 Notion 頁面 {stock_code} 價格為 {price}。")
            else:
                logger.debug(f"✔ 成功更新 Notion 頁面 {stock_code} 價格為 {price} (顏色: {color})。")
            
            return True # 更新成功，直接返回 True

        except Exception as e:
            logger.warning(f"更新 Notion 頁面 {stock_code} 失敗 (第 {attempt + 1}/{MAX_RETRIES} 次): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    # 如果三次重試都失敗了，才會執行到這裡
    logger.error(f"❌ 更新 Notion 頁面 {stock_code} 在重試 {MAX_RETRIES} 次後仍然徹底失敗。")

    # ⭐ 將失敗資訊加入列表
    failure_list.append({
        "type": "Notion Main DB Update",
        "stock_code": stock_code,
        "page_id": page_id,
        "reason": "嘗試所有次數後依舊沒成功更新主DB"
    })
    return False


def update_notion_watchlist(client: Client,order_id: str,page_title: str, status: str,failure_list: List[Dict]) -> bool:
    """【佔位】模擬更新用戶個人的 Watchlist 頁面。
    因為即時價格在主DB時就已經跟更新了，所以只需要更新status狀態(該code)
    先用流水號去找該流水號的DB_ID，再用DB_ID去抓title那row。
    最後再從那row找到狀態，並更新狀態。
    包含最多 3 次的 API 呼叫重試邏輯，第 3 次將使用備用 API Key。
    若為VVIP且有寄信的需求 才要寫一個email"""
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 0.5
    matching_user = next((user for user in config.USER_CONFIGS  if user["order"] == order_id), None)
    if not matching_user:
        logger.warning(f"訂單號為 {order_id} 沒有對這支股票需要做更新 => 沒在他有興趣的股票中")
        return False

    data_source_id = matching_user["DB_ID"]
    # --- 步驟一：用VIP的tilte去查詢頁面ID (帶有重試機制) ---
    results = None
    client_to_use = client # 預設使用傳入的 client

    for attempt in range(MAX_RETRIES):
        try:
            # 最後一次嘗試時，切換到備用 Key
            if attempt == MAX_RETRIES - 1:
                if config.RESERVE_NOTION_API_KEY_LIST:
                    logger.info(f"查詢 watchlist (order_id:{order_id}_page_title:{page_title}) 前兩次失敗，隨機選取備用 Notion Key...")
                    random_key = random.choice(config.RESERVE_NOTION_API_KEY_LIST)
                    client_to_use = Client(auth=random_key)
                else:
                    logger.warning(f"查詢 watchlist (order_id:{order_id}_page_title:{page_title}) 已達最後嘗試次數，但未設定備用 Key。")

            query_filter = {
                "property": "編號",
                "title": {"equals": page_title}
            }
            response = client_to_use.data_sources.query(
                data_source_id=data_source_id,
                filter=query_filter
            )
            results = response.get("results", [])
            break # 查詢成功，跳出重試迴圈

        except Exception as e:
            logger.warning(f"查詢 watchlist (order_id:{order_id}_page_title:{page_title}) 失敗 (第 {attempt + 1}/{MAX_RETRIES} 次): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    if results is None:
        logger.error(f"❌ 查詢 watchlist (order_id:{order_id}_page_title:{page_title}) 在重試 {MAX_RETRIES} 次後仍然徹底失敗。")
        # ⭐ 將失敗資訊加入列表
        failure_list.append({
            "type": "Notion Watchlist Query",
            "order_id": order_id,
            "page_title": page_title,
            "reason": "嘗試所有次數後，查詢 watchlist失敗."
        })
        return False

    if not results:
        logger.warning(f"在 watchlist ({order_id}) 中找不到編號為 '{page_title}' 的頁面。")
        return False


    # --- 步驟二：更新頁面狀態 (帶有重試機制) ---
    page_id_to_update = results[0]["id"]
    properties_to_update = {
        "狀態": {"status": {"name": status}}
    }

    client_to_use = client # 重置回預設的 client
    for attempt in range(MAX_RETRIES):
        try:
            if attempt == MAX_RETRIES - 1:
                if config.RESERVE_NOTION_API_KEY_LIST:
                    logger.info(f"更新 watchlist ({order_id}/{page_title}) 前兩次失敗，隨機選取備用 Notion Key...")
                    random_key = random.choice(config.RESERVE_NOTION_API_KEY_LIST)
                    client_to_use = Client(auth=random_key)
                else:
                    logger.warning(f"更新 watchlist ({order_id}/{page_title}) 已達最後嘗試次數，但未設定備用 Key。")

            client_to_use.pages.update(
                page_id=page_id_to_update,
                properties=properties_to_update
            )
            logger.debug(f"✔ Watchlist ({order_id}) 頁面 '{page_title}' 狀態成功更新為 '{status}'！")
            return True # 更新成功，直接返回 True

        except Exception as e:
            logger.warning(f"更新 watchlist ({order_id}/{page_title}) 失敗 (第 {attempt + 1}/{MAX_RETRIES} 次): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error(f"❌ 更新 watchlist ({order_id}/{page_title}) 在重試 {MAX_RETRIES} 次後仍然徹底失敗。")
    failure_list.append({
        "type": "Notion Watchlist Update",
        "order_id": order_id,
        "page_title": page_title,
        "page_id": page_id_to_update,
        "reason": "Failed to update page status after all retries."
    })
    return False




def get_stock_name(stock_code: str, at_root_file_name: str) -> Optional[str]:
    try:
        # 步驟一：計算出專案根目錄的路徑
        project_root = Path(__file__).parent.parent
        # 步驟二：【核心修正】使用 pathlib 的 / 運算子來安全地組合路徑
        json_path = project_root / f"{at_root_file_name}.json"
        
        if not json_path.exists():
            logger.error(f"錯誤：股票代碼對照表檔案 {json_path} 不存在")
            return None
        
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 【修正】先取出內層的 "stock_code_to_name_map" 字典
        code_map = data.get("stock_code_to_name_map", {})
        
        if not code_map:
            logger.warning(f"警告：{json_path} 中未找到 stock_code_to_name_map 鍵")
            return None
        return code_map.get(stock_code,None)
    except json.JSONDecodeError:
            logger.error(f"錯誤：{json_path} 檔案格式錯誤，無法解析 JSON")
            return None
    except Exception as e:
        logger.error(f"讀取 {json_path} 時發生未知錯誤: {e}")
        return None


def get_price_safely(
    worker_name: str,
    stock_code: str,
    api_key: str,
    max_retries: int = 3,
    retry_delay: int = 1
) -> Optional[Dict]:
    """
    一個共用的工具函式，負責以包含重試和備用 Key 的機制來抓取股票報價。
    成功時回傳完整的 response 字典，失敗時回傳 None。
    """
    client = RestClient(api_key=api_key, timeout=10)
    client_to_use = client # 預設使用傳入的 client
    
    for attempt in range(max_retries):
        try:
            # 在最後一次嘗試時，切換到備用 Key
            if attempt == max_retries - 1:
                if config.LATEST_CHANCE_STOCK_API_KEYS:
                    logger.info(f"[{worker_name}] {stock_code} 前兩次嘗試失敗，隨機選取備用 API Key...")
                    random_key = random.choice(config.LATEST_CHANCE_STOCK_API_KEYS)
                    client_to_use = RestClient(api_key=random_key, timeout=10)
            
            response = client_to_use.stock.intraday.quote(symbol=str(stock_code))

            if response.get('statusCode'):
                raise ValueError(f"API回傳伺服器端錯誤: {response.get('statusCode')}")
            
            logger.debug(f"[{worker_name}] {stock_code} 成功從富果取得回應。")
            return response # 成功，直接回傳整個 response

        except Exception as e:
            # 404 查無標的(多半已下市)：永久性錯誤，重試無意義，直接放棄
            if getattr(e, "status_code", None) == 404:
                logger.warning(f"[{worker_name}] {stock_code} 查無此標的 (404)，可能已下市，跳過不重試。")
                return None
            logger.warning(f"[{worker_name}] 抓取 {stock_code} 失敗 (第 {attempt + 1}/{max_retries} 次): {e}")
            if attempt < max_retries - 1:
                if config.RESERVE_STOCK_API_KEY:
                    random_key = random.choice(config.RESERVE_STOCK_API_KEY)
                    client_to_use = RestClient(api_key=random_key, timeout=10)
                    time.sleep(retry_delay)
                else:
                    client_to_use = client

    logger.error(f"❌ [{worker_name}] 抓取 {stock_code} 在重試 {max_retries} 次後仍然徹底失敗。")
    return None # 所有重試都失敗了


