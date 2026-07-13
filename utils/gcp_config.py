
# utils/gcp_config.py
import os
from google.cloud import secretmanager

def get_secret(project_id: str, secret_id: str, version_id: str = "latest") -> str:
    """
    從 GCP Secret Manager 中獲取 Secret 的值。
    """
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        # 在本地開發或沒有權限時，優雅地處理錯誤
        print(f"Could not access secret: {secret_id}. Error: {e}")
        return None

# --- 從環境變數或 GCP Secret Manager 讀取設定 ---
# 建議在 GCP VM 的環境變數中設定 PROJECT_ID
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "YOUR_GCP_PROJECT_ID_HERE") # 替換成您的 GCP Project ID

# 從 Secret Manager 讀取機密
NOTION_EMAIL = get_secret(GCP_PROJECT_ID, "NOTION_EMAIL")
NOTION_PASSWORD = get_secret(GCP_PROJECT_ID, "NOTION_PASSWORD")
ZOHO_SENDER_EMAIL = get_secret(GCP_PROJECT_ID, "ZOHO_SENDER_EMAIL")
ZOHO_APP_PASSWORD = get_secret(GCP_PROJECT_ID, "ZOHO_APP_PASSWORD")

# 讀取逗號分隔的 API Keys，並轉換為列表
fugle_keys_str = get_secret(GCP_PROJECT_ID, "FUGLE_API_KEYS")
STOCK_API_KEY = fugle_keys_str.split(',') if fugle_keys_str else []

notion_keys_str = get_secret(GCP_PROJECT_ID, "NOTION_API_KEYS")
NOTION_API_KEY_LIST = notion_keys_str.split(',') if notion_keys_str else []
