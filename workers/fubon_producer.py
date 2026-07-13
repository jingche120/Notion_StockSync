# workers/fubon_producer.py
"""
富邦 snapshot 抓價來源（取代 Fugle 逐支 quote）。

一次呼叫 `snapshot.quotes(market='TSE')` 即可拿回全部上市股票（實測約 1500+ 筆），
所以不需要多金鑰、多執行緒、節流與三層備援重試——一支單執行緒的生產者就夠了。

設計重點：
1. 登入一次、重用連線：module 層級單例，避免每分鐘重登被富邦限流。
2. 分鐘級去重：與「上一輪該股 lastPrice」比較，相同就 skip，省 Notion 寫入。
3. 顏色/漲跌幅直接來自快照：base_price = lastPrice - change（＝昨收），
   change_percent_raw = changePercent，不再依賴 df_base_data.json。

對外介面（與 main.py 的呼叫契約一致）：
    produce_with_fubon(task_q, master_vip_list, master_normal_list,
                       base_data, vip_database_dataframe_dist, api_notion_failure_q)
產出的 task 封包格式與 Fugle 路完全相同（共用 utils.task_builder.build_task_packet）。
"""
import logging
import queue
import random
import threading
from typing import Dict, List

import pandas as pd

from utils import config
from utils.task_builder import build_task_packet

logger = logging.getLogger(__name__)

# --- module 層級狀態（跨輪保存）---
_sdk = None                      # FubonSDK 單例
_reststock = None                # 行情 REST client（snapshot 由此呼叫）
_login_lock = threading.Lock()   # 保護登入流程（即使目前單執行緒，也先做對）
_last_price_seen: Dict[str, float] = {}  # 分鐘級去重：股票代碼 -> 上一輪 lastPrice


def _get_reststock():
    """取得（必要時先登入）富邦行情 REST client。登入只做一次、之後重用。"""
    global _sdk, _reststock
    if _reststock is not None:
        return _reststock

    with _login_lock:
        # double-check：可能在等鎖期間別的執行緒已登入完成
        if _reststock is not None:
            return _reststock

        # 延遲匯入：沒裝 fubon_neo 的環境（例如只跑 Fugle 路）不會在 import 時就壞掉
        from fubon_neo.sdk import FubonSDK

        if not (config.FUBON_ACCOUNT and config.FUBON_PASSWORD and config.FUBON_CERT_PATH):
            raise RuntimeError(
                "富邦憑證未設定完整，請在 .env 設定 FUBON_ACCOUNT / FUBON_PASSWORD / FUBON_CERT_PATH。"
            )

        logger.info("[富邦] 首次使用，開始登入並建立行情連線...")
        sdk = FubonSDK()
        accounts = sdk.login(
            config.FUBON_ACCOUNT,
            config.FUBON_PASSWORD,
            config.FUBON_CERT_PATH,
        )
        if not (accounts and accounts.is_success):
            raise RuntimeError(f"富邦登入失敗：{accounts}")

        sdk.init_realtime()  # 建立行情連線（snapshot 需要）
        _sdk = sdk
        _reststock = sdk.marketdata.rest_client.stock
        logger.info(f"[富邦] 登入成功，帳號：{accounts.data[0]}")
        return _reststock


def _fetch_snapshot_lookup() -> Dict[str, dict]:
    """逐一查詢 config.FUBON_SNAPSHOT_MARKETS 的各市場別快照，合併成單一 {symbol: item} 查表。

    創新板(TIB)、上櫃(OTC)、興櫃(ESB)各自獨立於上市(TSE)，需分別查詢再合併。
    某個市場別查詢失敗只略過該板（不影響其他板），全部失敗才回傳空 dict。
    """
    reststock = _get_reststock()
    # 延遲匯入富邦的例外型別
    from fubon_neo.fugle_marketdata.rest.base_rest import FugleAPIError

    lookup: Dict[str, dict] = {}
    for market in config.FUBON_SNAPSHOT_MARKETS:
        try:
            result = reststock.snapshot.quotes(market=market)
        except FugleAPIError as e:
            status = getattr(e, "status_code", "N/A")
            logger.error(f"[富邦] {market} 快照查詢失敗（status={status}）：{e}")
            continue

        data = result.get("data", []) if isinstance(result, dict) else []
        added = 0
        for item in data:
            if "symbol" in item:
                lookup[item["symbol"]] = item
                added += 1
        logger.info(f"[富邦] {market} 快照取得 {added} 檔。")

    logger.info(f"[富邦] 各市場別合併後共 {len(lookup)} 檔可查。")
    return lookup


def _emit_packet(
    stock_code: str,
    item: dict,
    is_vip: bool,
    base_data: Dict[str, float],
    vip_database_dataframe_dist: Dict[str, pd.DataFrame],
    task_q: queue.Queue,
) -> None:
    """把單檔快照資料 → 分鐘級去重 → build_task_packet → 丟進 task_q。"""
    # lastPrice 可能在「整天無成交」時缺漏，退而用 closePrice
    last_price = item.get("lastPrice")
    if last_price is None:
        last_price = item.get("closePrice")
    if last_price is None:
        # 真的拿不到價格，當作這檔本輪無資料（不丟佇列，由呼叫端記失敗）
        raise ValueError(f"{stock_code} 快照缺少 lastPrice/closePrice")

    last_price = float(last_price)

    # 測試用：盤後快照為靜態價，開啟模擬後每檔各有 0.5 機率 +1，製造跨輪價格變動。
    # 只動 lastPrice，不重算 changePercent（測試階段忽略漲跌幅準確性）。
    if config.SIMULATE_PRICE_CHANGE and random.random() < 0.5:
        last_price += 1

    change = item.get("change")
    change_percent = item.get("changePercent")

    # --- 分鐘級去重：跟上一輪一樣就跳過 ---
    prev = _last_price_seen.get(stock_code)
    if prev is not None and prev == last_price:
        return
    _last_price_seen[stock_code] = last_price

    # base_price 優先用快照自洽推算（lastPrice - change ＝ 昨收）；缺 change 時退用昨收檔
    if change is not None:
        base_price = last_price - float(change)
    else:
        base_price = float(base_data.get(stock_code, last_price))

    task_packet = build_task_packet(
        stock_code=stock_code,
        current_price=last_price,
        base_price=base_price,
        change_percent_raw=change_percent,
        is_vip=is_vip,
        vip_database_dataframe_dist=vip_database_dataframe_dist,
        vvip_list=config.VVIP_ORDERS,
    )
    # 價格與昨收相同（平盤）時 build_task_packet 回傳 None → 不丟佇列
    if task_packet is not None:
        logger.debug(f"[富邦] task_packet {task_packet}")
        task_q.put(task_packet)


def produce_with_fubon(
    task_q: queue.Queue,
    master_vip_list: List[str],
    master_normal_list: List[str],
    base_data: Dict[str, float],
    vip_database_dataframe_dist: Dict[str, pd.DataFrame],
    api_notion_failure_q: queue.Queue,
) -> None:
    """抓價來源＝富邦快照：一次抓全市場，查表後逐支打包丟進 task_q。"""
    failed_stocks: List[str] = []

    lookup = _fetch_snapshot_lookup()
    if not lookup:
        # 快照整批失敗：把這輪所有股票記為失敗，讓上游 log
        failed_stocks.extend(master_vip_list)
        failed_stocks.extend(master_normal_list)
        if failed_stocks:
            api_notion_failure_q.put(failed_stocks)
        logger.error("[富邦] 本輪快照無資料，跳過所有股票更新。")
        return

    # VIP 優先、再普通；is_vip 決定封包是否帶 user_details
    for stock_code, is_vip in (
        [(c, True) for c in master_vip_list] + [(c, False) for c in master_normal_list]
    ):
        item = lookup.get(stock_code)
        if item is None:
            logger.warning(f"[富邦] 快照中查無 {stock_code}（可能非上市/已下市），跳過。")
            failed_stocks.append(stock_code)
            continue
        try:
            _emit_packet(
                stock_code, item, is_vip, base_data, vip_database_dataframe_dist, task_q
            )
        except Exception as e:
            logger.error(f"[富邦] 處理 {stock_code} 時發生錯誤：{e}")
            failed_stocks.append(stock_code)

    if failed_stocks:
        api_notion_failure_q.put(failed_stocks)
        logger.info(f"[富邦] 本輪回報 {len(failed_stocks)} 筆失敗/查無項目。")

    logger.info("[富邦] 本輪快照生產完成。")
