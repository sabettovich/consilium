import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env if present
env_path = Path(__file__).resolve().parents[1] / '.env'
if env_path.exists():
    load_dotenv(env_path)

# Base settings
BASE_ID_URL = os.getenv('BASE_ID_URL', 'http://localhost:8000')
DB_PATH = os.getenv('CONSILIUM_DB_PATH', str(Path(__file__).resolve().parents[1] / 'data' / 'consilium.db'))

# Google Drive
GDRIVE_ROOT_FOLDER_ID = os.getenv('GDRIVE_ROOT_FOLDER_ID', '')
GDRIVE_ROOT_PATH = os.getenv('GDRIVE_ROOT_PATH', '/Matters')
GDRIVE_OAUTH_CLIENT = os.path.expanduser(os.getenv('GDRIVE_OAUTH_CLIENT', ''))
GDRIVE_OAUTH_TOKEN = os.path.expanduser(os.getenv('GDRIVE_OAUTH_TOKEN', ''))

# Notifications (minimal stub)
NOTIF_ENABLE = os.getenv('NOTIF_ENABLE', '1') in ('1', 'true', 'True')
NOTIF_LOG_PATH = os.getenv('NOTIF_LOG_PATH', str(Path(__file__).resolve().parents[1] / 'logs' / 'notifications.log'))

# Email (SMTP)
SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587')) if os.getenv('SMTP_PORT') else None
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
EMAIL_FROM = os.getenv('EMAIL_FROM', '')
EMAIL_TO = os.getenv('EMAIL_TO', '')

# Matrix
MATRIX_HOMESERVER = os.getenv('MATRIX_HOMESERVER', '')  # e.g., https://matrix-client.matrix.org
MATRIX_USER = os.getenv('MATRIX_USER', '')
MATRIX_ACCESS_TOKEN = os.getenv('MATRIX_ACCESS_TOKEN', '')
MATRIX_ROOM_ID = os.getenv('MATRIX_ROOM_ID', '')

# Integrity report (B1)
INTEGRITY_INTERVAL_MIN = int(os.getenv('INTEGRITY_INTERVAL_MIN', '60'))  # minutes
INTEGRITY_BATCH = int(os.getenv('INTEGRITY_BATCH', '50'))
INTEGRITY_INCLUDE_STATUSES = os.getenv('INTEGRITY_INCLUDE_STATUSES', 'registered,delivered')

# Embed metadata (B2)
EMBED_MODE = os.getenv('EMBED_MODE', 'revision')  # revision|copy|sidecar
EMBED_OUT_FOLDER = os.getenv('EMBED_OUT_FOLDER', 'Client_Share')
EMBED_ON_DELIVER = os.getenv('EMBED_ON_DELIVER', 'false').lower() == 'true'

# Client read token (B3)
CLIENT_READ_TOKEN = os.getenv('CLIENT_READ_TOKEN', '')

# Docassemble hook token (B5)
DOCASSEMBLE_HOOK_TOKEN = os.getenv('DOCASSEMBLE_HOOK_TOKEN', '')
