"""
一次性把所有股票「種」進主資料庫（即時股價資料庫）。

背景：主資料庫的更新邏輯是「先查詢既有頁面 → 再更新」，從不 create。
所以重建後的空資料庫必須先用這支腳本把每一支股票建立成一筆頁面，
之後 main.py 才有東西可以更新。

- 代碼來源：df_base_data.json 的 key（= main.py 實際會處理的 1293 支）
- 名稱來源：stock_code_to_name_map.json（巢狀 {"stock_code_to_name_map": {代碼: 名稱}}），查不到就用代碼當名稱
- 寫入端點：新版 data_sources（parent 用 data_source_id）
- 僅在「空資料庫」第一次執行；重複執行會產生重複頁面

用法：
    python seed_main_db.py            # 真的寫入
    python seed_main_db.py --dry-run  # 只試算、不寫入
"""
import os
import sys
import json
import time
import logging
from dotenv import load_dotenv
from notion_client import Client

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logging.disable(logging.WARNING)  # 安靜掉 notion_client 的 request log

from utils import config

BASE_DATA_FILE = "df_base_data.json"
NAME_MAP_FILE = "stock_code_to_name_map.json"
RATE_LIMIT_DELAY = 0.35  # Notion 每秒約 3 次請求，留安全邊界


def build_properties(stock_name: str, stock_code: str) -> dict:
    """組裝單一頁面的 properties，欄位對齊主資料庫 schema。"""
    return {
        "股票名稱": {"title": [{"text": {"content": stock_name}}]},
        "股票代碼": {"rich_text": [{"text": {"content": stock_code}}]},
        "標記": {"multi_select": [{"name": "所有股票"}]},
        "即時價格": {"rich_text": [{"text": {"content": "--"}}]},
        "漲跌幅": {"rich_text": [{"text": {"content": "--"}}]},
    }


def load_codes_and_names():
    """回傳 [(code, name), ...]，以 df_base_data 的代碼為準。"""
    with open(BASE_DATA_FILE, "r", encoding="utf-8") as f:
        base = json.load(f)
    with open(NAME_MAP_FILE, "r", encoding="utf-8") as f:
        name_map = json.load(f).get("stock_code_to_name_map", {})

    pairs = []
    for code in base.keys():
        name = name_map.get(code, code)  # 查不到名字就用代碼當名稱
        pairs.append((code, name))
    return pairs


def main():
    dry_run = "--dry-run" in sys.argv

    api_key = config.NOTION_API_KEY_LIST[0].strip()
    data_source_id = config.MAIN_DATABASE_ID
    client = Client(auth=api_key)

    pairs = load_codes_and_names()
    total = len(pairs)
    fallback = sum(1 for code, name in pairs if name == code)  # 查不到名字、用代碼當名稱的筆數
    print(f"準備種入主資料庫 data_source={data_source_id}")
    print(f"共 {total} 支股票（代碼以 {BASE_DATA_FILE} 為準，其中 {fallback} 支查無名稱、以代碼代替）")
    if dry_run:
        print("\n[DRY-RUN] 只試算、不寫入。前 5 筆預覽：")
        for code, name in pairs[:5]:
            print(f"  {code} -> {name}")
        print(f"\n預估耗時約 {total * RATE_LIMIT_DELAY / 60:.1f} 分鐘")
        return

    success, failed = 0, []
    for i, (code, name) in enumerate(pairs, start=1):
        try:
            client.pages.create(
                parent={"type": "data_source_id", "data_source_id": data_source_id},
                properties=build_properties(name, code),
            )
            success += 1
            if i % 50 == 0 or i == total:
                print(f"  ({i}/{total}) 已寫入 {success} 筆…")
        except Exception as e:
            failed.append((code, name, str(e)))
            print(f"  ({i}/{total}) ✗ {code} {name} 失敗：{e}")
        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n完成：成功 {success} 筆，失敗 {len(failed)} 筆。")
    if failed:
        print("失敗清單：")
        for code, name, err in failed:
            print(f"  - {code} {name}：{err}")


if __name__ == "__main__":
    main()
