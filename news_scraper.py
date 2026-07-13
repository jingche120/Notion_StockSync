# news_scraper.py
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from lxml import etree
from utils.config import NEWS_BLOCK_ID,NOTION_API_KEY_LIST,TPE_INDEX_ID
from notion_client import Client, APIResponseError
from datetime import datetime
import random
import logging
logger = logging.getLogger(__name__)

def scraper_news_and_index():
    client = Client(auth = NOTION_API_KEY_LIST[0])
    driver = None # 先將 driver 初始化為 None
    try:
        # --- 設定 Chrome 選項 ---
        chrome_options = Options()
        chrome_options.add_argument('--headless') # 在背景執行
        chrome_options.add_argument("--incognito") # 使用無痕模式
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        # 為了與 NotionCrawler 的設定保持一致，也加入了 user-agent 和語言設定
        chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        chrome_options.add_experimental_option('prefs', {'intl.accept_languages': 'zh-TW,zh'})
        chrome_options.add_argument("--lang=zh-TW")
        driver = webdriver.Chrome(options=chrome_options)

        # -----------------------------------------------------------
        # 抓加權指數
        url = "https://www.google.com/finance/quote/IX0001:TPE?hl=zh-TW"
        driver.get(url)
        wait = WebDriverWait(driver, 10)
        element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '.YMlKec.fxKbKc')))
        title_txt = datetime.strftime(datetime.now(),'%m-%d %H:%M')
        # 透過 .text 屬性取得元素內的文字
        tpe_index_text= element.text
        tpe_index_text = tpe_index_text.replace(",", "")
        new_page_properties = {
                # 1. Title (標題) 屬性 —— 資料庫標題欄為 '時間點'
                # 格式固定，'title' 是 key，內容是 list of text objects
                '時間點': {
                    'title': [
                        {
                            'text': {
                                'content': title_txt
                            }
                        }
                    ]
                },
                # 2. Rich Text (多行文本) 屬性 —— '值' 欄位型別為 rich_text
                '值': {'rich_text': [{'text': {'content': tpe_index_text}}]},
            }
        for i in range(3):
            try:
                response = client.pages.create(
                    parent={"type": "data_source_id", "data_source_id": TPE_INDEX_ID},
                    properties=new_page_properties
                )
                logger.info(f"✔ 本輪查詢大盤指數成功，並放在Notion上面")
                break  # 成功了，就用 break 跳出迴圈

            # 建議捕捉更精準的 APIResponseError，而不是籠統的 Exception
            except Exception as e:
                logger.warning(f"第 {i+1}/3 次查詢大盤指數失敗: {e}")
                # 在最後一次嘗試失敗前都等待一下
                if i < 2:
                    time.sleep(1) # 等待 1 秒再重試
                else:
                    logger.error(f"❌查詢大盤指數失敗: {e}")

        # 抓即時新聞
        url = 'https://www.google.com/finance/?hl=zh-TW'
        driver.get(url)
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, 'yY3Lee')))
        logger.info("抓即時新聞_動態內容已成功載入！")
        html_content = driver.page_source
        html_tree = etree.HTML(html_content)
        xpath_expression = '//section[@aria-labelledby="news-title"]//div[@class="yY3Lee"]'
        elements = html_tree.xpath(xpath_expression)
        # 7. 遍歷並印出結果
        if elements:
            logger.info(f"成功找到了 {len(elements)} 則新聞。\n")
            for i, element in enumerate(elements, 0):
                if i>=5:
                    break
                # 使用 XPath 的 string(.) 函式來提取所有文字，這是一個更穩健的方法
                # .strip() 可以清除前後多餘的空白或換行
                text_content = element.xpath("string(.)").strip() 
                # 【新增】抓取 <a> 標籤的 href 屬性
                # .//a/@href 會尋找當前 element 底下任何的 <a> 標籤，並回傳其 href 屬性值
                # xpath 回傳的是一個列表，所以我們取第一個元素
                link_list = element.xpath(".//a/@href")
                link = link_list[0] if link_list else "連結不存在"
                log_detail = f"""
                正在處理第 {i} 則新聞:
                    - 標題: {text_content}
                    - 連結: {link}
                    - Notion Block ID: {NEWS_BLOCK_ID[i]}
                """
                logger.debug(log_detail)

                try:
                    logger.info(f"BlockID={NEWS_BLOCK_ID[i]}")
                    response = client.blocks.update(
                        block_id=NEWS_BLOCK_ID[i],
                        # 這裡的結構和我們之前手動組合的 JSON Payload 幾乎一樣
                        # 但現在是作為 Python 的字典 (dict) 傳遞
                        bulleted_list_item={
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {
                                        "content": text_content,
                                        "link": {"url": link},
                                    },
                                }
                            ]
                        }
                    )            
                except APIResponseError as e:
                    logger.error("❌ 抓即時新聞_Notion API 回傳了一個錯誤！")
                    # print(f"錯誤代碼 (Error Code): {e.code}")
                    # print(f"錯誤訊息 (Message): {e.message}")
                    # e.body 包含了 Notion 回傳的最完整的 JSON 錯誤訊息，非常有用
                    logger.error(f"抓即時新聞_完整回應 (Full Body): {e}") 

            else:
                logger.error("抓即時新聞_雖然等到元素出現，但在最終解析時沒有找到。請檢查 XPath。")

    finally:
        # 8. 關閉瀏覽器，釋放資源
        # 加上這個判斷，確保 driver 成功建立後才執行 quit
        if driver:
            driver.quit()