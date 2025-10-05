import os
import io
import mimetypes
from typing import Dict
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from .config import GDRIVE_OAUTH_CLIENT, GDRIVE_OAUTH_TOKEN

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

_service = None

def get_service():
    global _service
    if _service is not None:
        return _service
    if not GDRIVE_OAUTH_CLIENT:
        raise RuntimeError("GDRIVE_OAUTH_CLIENT not configured")
    # expand $HOME and ~ for paths from env
    def _expand(p: str) -> str:
        return os.path.expanduser(os.path.expandvars(p))

    token_path = _expand(GDRIVE_OAUTH_TOKEN) if GDRIVE_OAUTH_TOKEN else None
    creds = None
    if token_path and os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_expand(GDRIVE_OAUTH_CLIENT), SCOPES)
            creds = flow.run_local_server(port=0)
        if token_path:
            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            with open(token_path, "w") as f:
                f.write(creds.to_json())
    _service = build("drive", "v3", credentials=creds)
    return _service

def find_or_create_folder(name: str, parent_id: str) -> str:
    svc = get_service()
    q = (
        f"name = '{name.replace("'", "\\'")}' and '{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    r = svc.files().list(q=q, fields="files(id,name)").execute()
    items = r.get("files", [])
    if items:
        return items[0]["id"]
    body = {"name": name, "parents": [parent_id], "mimeType": "application/vnd.google-apps.folder"}
    f = svc.files().create(body=body, fields="id").execute()
    return f["id"]

def upload_file(parent_id: str, local_path: str, target_name: str) -> Dict[str, str]:
    svc = get_service()
    mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    media = MediaFileUpload(local_path, mimetype=mime, resumable=False)
    body = {"name": target_name, "parents": [parent_id]}
    f = svc.files().create(body=body, media_body=media, fields="id,webViewLink").execute()
    return {"id": f["id"], "webViewLink": f.get("webViewLink")}

def update_file_content(file_id: str, local_path: str) -> Dict[str, str]:
    """Upload new content for existing fileId (creates a new version)."""
    svc = get_service()
    mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    media = MediaFileUpload(local_path, mimetype=mime, resumable=False)
    f = svc.files().update(fileId=file_id, media_body=media, fields="id,webViewLink").execute()
    return {"id": f["id"], "webViewLink": f.get("webViewLink")}

def download_file_content(file_id: str) -> bytes:
    svc = get_service()
    request = svc.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return fh.read()

def get_file_webview_link(file_id: str) -> str | None:
    svc = get_service()
    meta = svc.files().get(fileId=file_id, fields="webViewLink").execute()
    return meta.get("webViewLink")

def get_file_name_mime(file_id: str) -> Dict[str, str]:
    svc = get_service()
    meta = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
    return {"name": meta.get("name", ""), "mimeType": meta.get("mimeType", "")}
