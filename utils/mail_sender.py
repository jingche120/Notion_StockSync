# utils/mail_sender.py
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class EmailSender:
    """
    一個使用 mail 的 SMTP 服務來寄送郵件的物件。
    """
    def __init__(self, sender_email: str, app_password: str,smtp_server: str = "smtp.zoho.com",smtp_port: int = 465):
        """
        初始化寄送器。

        Args:
            sender_email (str): 您用來寄信的地址 (例如您的 Zoho 信箱)。
            app_password (str): 您的 16 位元應用程式密碼。
            smtp_server (str): SMTP 伺服器位址。
            smtp_port (int): SMTP 伺服器埠號 (465 for SSL)。
        """
        if not sender_email or not app_password:
            raise ValueError("寄件人信箱和應用程式密碼不能為空。")
        self.sender_email = sender_email
        self.app_password = app_password
        self.smtp_server = smtp_server  # <--- 錯誤很可能就是少了這一行
        self.smtp_port = smtp_port      # <--- 或是少了這一行

    def send_email(
        self,
        recipient: str,
        alert_data: Dict[str, Any]
    ) -> bool:
        """
        為【單一警示】，產生一封通知郵件並寄送。

        Args:
            recipient (str): 收件人信箱。
            alert_data (Dict[str, Any]): 包含【單一】警報資訊的字典。
        """
        if not alert_data:
            logger.warning("沒有提供任何警報資料，郵件未寄送。")
            return False

        # --- 步驟一：動態產生郵件主旨 ---
        logger.debug("準備動態產生郵件主旨...")

        stock_name = alert_data.get("股票名稱", "")
        stock_code = alert_data.get("股票代碼", "N/A")
        subject = f"【Notion台股提醒】價格警示：{stock_name} ({stock_code})"


        # --- 步驟二：動態建立 HTML 郵件內文 ---
        body_html = f"""
        <html><body>
            <p>尊敬的用戶：</p>
            <p>特此通知，您所設定的觀察股 {stock_name} ({stock_code})，其目前市價已觸動您設定的價格條件。</p>
            <p>詳細資訊如下：</p>
            <pre style="font-family:monospace; border:1px solid #ddd; padding:10px; background-color:#f9f9f9;">
            ----------------------------------
            股票標的： {stock_name} ({stock_code})
            觸發條件： {alert_data.get('觸發狀態', 'N/A')}
            您的目標值_低： {alert_data.get('目標價格_低', 'N/A')} 元
            您的目標值_高： {alert_data.get('目標價格_高', 'N/A')} 元
            即時價格： {alert_data.get('即時價格', 'N/A')} 元
            ----------------------------------
            </pre>
            <p>建議您登入您的券商平台，以獲取最即時的報價並評估後續操作。</p><hr>
            <p><b>溫馨提醒：</b><br>若您未取消"email"提醒，在股價持續符合條件期間，系統將會定期寄送本通知信。</p>

            <p><b>免責聲明：</b><br>本服務資訊僅供參考，不構成投資建議。所有股價資訊應以券商實際成交價為準。</p>
            <p>如有任何問題，請聯絡<a href="mailto:service@soulation.store"><i>service@soulation.store</i></a></p>
            <small>(這是一封提醒信件，您可以直接回覆)</small>
        </body></html>
        """
        msg = MIMEText(body_html, 'html', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = self.sender_email
        msg['To'] = recipient

        # --- 步驟四：連線到 SMTP 伺服器並寄送 ---
        try:
            logger.debug("準備連線到 SMTP 伺服器並寄送...")
            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port) as server:
                server.login(self.sender_email, self.app_password)
                server.sendmail(self.sender_email, recipient, msg.as_string())
                logger.info(f"✔郵件成功寄送至: {recipient}")
                return True
        except Exception as e:
            logger.error(f"❌寄送郵件時發生錯誤: {e}")
            return False
        

    # def send_for_notion_login_fail(
    #     self,
    #     recipient: str,
    # ) -> bool:
    #     """
    #     為【單一警示】，產生一封通知郵件並寄送。

    #     Args:
    #         recipient (str): 收件人信箱。
    #         alert_data (Dict[str, Any]): 包含【單一】警報資訊的字典。
    #     """

    #     # --- 步驟一：動態產生郵件主旨 ---
    #     logger.info("[注意]notion_login_fail")

    #     subject = f"【notion_login_fail】這輪({datetime.now().strftime('%Y-%m-%d %H:%M')})無法登入notion，請查看log檔案。"


    #     # --- 步驟二：動態建立 HTML 郵件內文 ---
    #     body_html = f"""
    #         無法登入notion。
    #     """
    #     msg = MIMEText(body_html, 'html', 'utf-8')
    #     msg['Subject'] = subject
    #     msg['From'] = self.sender_email
    #     msg['To'] = recipient

    #     # --- 步驟四：連線到 SMTP 伺服器並寄送 ---
    #     try:
    #         with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port) as server:
    #             server.login(self.sender_email, self.app_password)
    #             server.sendmail(self.sender_email, recipient, msg.as_string())
    #             logger.info(f"✔郵件成功寄送至: {recipient}")
    #             return True
    #     except Exception as e:
    #         logger.error(f"❌寄送郵件時發生錯誤: {e}")
    #         return False        