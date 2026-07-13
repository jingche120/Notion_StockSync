"""
共用的「任務封包」打包邏輯。

把「價格 → 紅綠/漲跌幅 → VIP 到價判斷 → 組成 task 封包」這段業務邏輯，
從 API_Worker._process_stock 抽出來成純函式，讓不同的抓價來源
（Fugle 逐支 quote / 富邦 snapshot）都能重用、不重複實作。

封包格式（與下游 Notion_update_worker 的介面契約）：
- 普通股票： [股票代碼, 價格, 顏色, 漲跌幅]
- VIP 股票： [股票代碼, 價格, 顏色, 漲跌幅, user_details_list]   ← 多一個元素
顏色： "RED"(漲) / "GREEN"(跌)
"""
import logging
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _build_vip_user_details(
    stock_code: str,
    current_price: float,
    vip_database_dataframe_dist: Dict[str, pd.DataFrame],
    vvip_list: List[str],
) -> List[dict]:
    """遍歷每位 VIP 的自選股 DataFrame，對有追蹤這支股票的用戶做到價判斷，組出 user_details。"""
    user_details_list: List[dict] = []

    for order_id, user_df in vip_database_dataframe_dist.items():
        target_row = user_df[user_df['股票代碼'] == stock_code]
        if target_row.empty:
            continue

        stock_setting = target_row.iloc[0]
        # page_title(編號) 用來在更新 VIP 自選股時定位該列（該處不能用 code）
        page_title = stock_setting['編號']
        high_str = str(stock_setting['目標價_高']).replace('$', '').replace(',', '')
        low_str = str(stock_setting['目標價_低']).replace('$', '').replace(',', '')
        note = stock_setting['備註']

        # 安全轉型：欄位空白或格式錯誤時預設 0.0
        try:
            target_high = float(high_str) if high_str else 0.0
        except ValueError:
            target_high = 0.0
            logger.warning(f"無法轉換目標價_高 '{stock_setting['目標價_高']}' 為數字，已設為 0。")
        try:
            target_low = float(low_str) if low_str else 0.0
        except ValueError:
            target_low = 0.0
            logger.warning(f"無法轉換目標價_低 '{stock_setting['目標價_低']}' 為數字，已設為 0。")

        # 防呆：高低顛倒就交換
        if target_high < target_low:
            target_high, target_low = target_low, target_high

        # 到價判斷
        if current_price >= target_high:
            status = "股價高於目標價_高"
        elif current_price <= target_low:
            status = "股價低於目標價_低"
        else:
            status = "股價在目標價之間"

        detail = {
            'order_id': order_id,
            'page_title': page_title,
            'target_high': target_high,
            'target_low': target_low,
            'note': note,
            'status': status,
        }

        # VVIP 且該行「是否通知」== email → 額外標記 email_needed，下游才會寄信
        if order_id in vvip_list and stock_setting.get('是否通知') == 'email':
            detail['email_needed'] = True

        user_details_list.append(detail)

    return user_details_list


def build_task_packet(
    stock_code: str,
    current_price: float,
    base_price: float,
    change_percent_raw,
    is_vip: bool,
    vip_database_dataframe_dist: Dict[str, pd.DataFrame],
    vvip_list: List[str],
) -> Optional[list]:
    """
    組出單一股票的 task 封包。

    回傳：
    - 普通： [股票代碼, 價格, 顏色, 漲跌幅]
    - VIP（有人追蹤）： 上面再加 user_details_list
    - **若價格與基準價相同（無變化）→ 回傳 None**（沿用原行為：不更新、不丟進佇列）

    change_percent_raw：API 直接給的漲跌幅數字（例如 1.15 代表 +1.15%）；
    若為 None 則用 (現價-基準)/基準 自行計算。
    """
    current_price = float(current_price)
    base_price = float(base_price)

    # 價格沒變就不更新（保留原 _process_stock 的行為）
    if current_price == base_price:
        return None

    if change_percent_raw is not None:
        price_change_percent = f"{float(change_percent_raw):.2f}%"
    else:
        if base_price > 0:
            raw_change = (current_price - base_price) / base_price
            price_change_percent = f"{raw_change:.2%}"
        else:
            price_change_percent = "N/A"

    color = "RED" if current_price > base_price else "GREEN"
    task_packet = [stock_code, current_price, color, price_change_percent]

    if is_vip:
        user_details_list = _build_vip_user_details(
            stock_code, current_price, vip_database_dataframe_dist, vvip_list
        )
        if user_details_list:
            task_packet.append(user_details_list)

    return task_packet