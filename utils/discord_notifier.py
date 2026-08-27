# utils/discord_notifier.py
import logging
import requests

from utils import config

logger = logging.getLogger()


def send_discord_message(content: str) -> bool:
    """透過 Discord Webhook 發送一則文字訊息。失敗時只記 log，不拋出例外。"""
    if not config.DISCORD_WEBHOOK_URL:
        logger.warning("未設定 DISCORD_WEBHOOK_URL，略過 Discord 通知。")
        return False

    try:
        response = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json={"content": content},
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Discord 通知已送出。")
        return True
    except Exception as e:
        logger.error(f"❌ 發送 Discord 通知時發生錯誤: {e}", exc_info=True)
        return False
