# Helpers
def _is_audio(filename: str | None, content_type: str | None) -> bool:
    ctype = (content_type or "").lower()
    if ctype.startswith("audio/"):
        return True
    if filename:
        _, ext = os.path.splitext(filename)
        if ext.lower() in {".wav", ".mp3"}:
            return True
    return False

# Upload constraints (C1.2)
UPLOAD_MAX_BYTES_DEFAULT = 25 * 1024 * 1024  # 25 MB
UPLOAD_MAX_BYTES_AUDIO = 100 * 1024 * 1024   # 100 MB for audio evidence
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "text/plain",
    "image/png",
    "image/jpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",  # mp3
}
ALLOWED_EXTS = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".wav", ".mp3"}

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select, desc
from pathlib import Path
import tempfile
import os
import json
from datetime import datetime
import asyncio
from typing import Optional, Dict, List
import base64
import mimetypes
import requests
import subprocess
import sys

from .db import Base, engine, SessionLocal
from .models import Doc, Matter
from .resolver import generate_doc_id, compute_sha256, build_permalink
from .config import (
    GDRIVE_ROOT_FOLDER_ID,
    GDRIVE_ROOT_PATH,
    NOTIF_ENABLE,
    NOTIF_LOG_PATH,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASS,
    EMAIL_FROM,
    EMAIL_TO,
    MATRIX_HOMESERVER,
    MATRIX_ACCESS_TOKEN,
    MATRIX_ROOM_ID,
    INTEGRITY_INTERVAL_MIN,
    INTEGRITY_BATCH,
    INTEGRITY_INCLUDE_STATUSES,
    EMBED_MODE,
    EMBED_OUT_FOLDER,
    EMBED_ON_DELIVER,
    CLIENT_READ_TOKEN,
    DOCASSEMBLE_HOOK_TOKEN,
)
from . import drive as gdrive
from .notifier_email import send_email
from .notifier_matrix import send_matrix_message


app = FastAPI(title="Consilium Resolver", version="0.1.0")
templates = Jinja2Templates(directory="templates")

# Auto-migrate (create tables)
Base.metadata.create_all(bind=engine)

# --- Pydantic models (used in endpoints) ---
class PatchDoc(BaseModel):
    title: str | None = None
    storage: str | None = None
    storage_ref: str | None = None
    status: str | None = None
    tags: dict | list | None = None
    owner: str | None = None
    origin: str | None = None
    origin_meta: dict | list | None = None


class DocassembleHook(BaseModel):
    matter_id: str
    title: str
    class_: str | None = None
    file_base64: str | None = None  # base64-encoded file content
    file_url: str | None = None     # alternatively, URL to download
    origin_meta: dict | list | None = None

# --- Lightweight migrations for SQLite (idempotent) ---
def _add_column_if_missing(table: str, column: str, decl: str) -> None:
    from sqlalchemy import text
    with engine.connect() as conn:
        info = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        cols = {row[1] for row in info}
        if column not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))

# Add new columns for matters
_add_column_if_missing("matters", "client_name", "TEXT")
_add_column_if_missing("matters", "status", "TEXT")
_add_column_if_missing("matters", "tags", "JSON")
_add_column_if_missing("matters", "folder_path", "TEXT")
_add_column_if_missing("matters", "created_at", "DATETIME")
_add_column_if_missing("matters", "updated_at", "DATETIME")

# Add new columns for docs
_add_column_if_missing("docs", "origin", "TEXT")
_add_column_if_missing("docs", "origin_meta", "JSON")
_add_column_if_missing("docs", "owner", "TEXT")
_add_column_if_missing("docs", "status", "TEXT")
_add_column_if_missing("docs", "tags", "JSON")
_add_column_if_missing("docs", "updated_at", "DATETIME")


# --- B1: Integrity report (background verify + endpoint) ---

REPORTS_DIR = Path("logs")
INTEGRITY_REPORT_PATH = REPORTS_DIR / "reports_integrity.jsonl"


def _integrity_statuses() -> List[str]:
    return [s.strip() for s in INTEGRITY_INCLUDE_STATUSES.split(",") if s.strip()]


def _write_integrity_record(rec: dict) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with INTEGRITY_REPORT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def _run_integrity_batch() -> int:
    """Verify a batch of docs and write JSONL records. Returns number processed."""
    count = 0
    statuses = _integrity_statuses()
    with SessionLocal() as db:
        q = select(Doc).where(Doc.status.in_(statuses)).limit(INTEGRITY_BATCH)
        rows = db.execute(q).scalars().all()
        for row in rows:
            rec = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "doc_id": row.doc_id,
                "matter_id": row.matter_id,
                "status": row.status,
            }
            try:
                if row.storage != "gdrive" or not row.storage_ref:
                    raise RuntimeError("Unsupported storage or missing ref")
                content = gdrive.download_file_content(row.storage_ref)
                import hashlib
                h = hashlib.sha256(); h.update(content)
                current = h.hexdigest()
                rec["result"] = {
                    "match": bool(current == row.sha256_plain),
                    "sha256_current": current,
                    "sha256_stored": row.sha256_plain,
                }
            except Exception as e:
                rec["error"] = str(e)
            _write_integrity_record(rec)
            count += 1
    return count


async def _integrity_worker():
    # Run periodic batches
    interval = max(1, INTEGRITY_INTERVAL_MIN) * 60
    while True:
        try:
            await _run_integrity_batch()
        except Exception:
            # не роняем сервер из-за фоновой задачи
            pass
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _startup_tasks():
    # fire-and-forget background task
    asyncio.create_task(_integrity_worker())


@app.get("/api/reports/integrity")
def get_integrity_report(
    matter_id: Optional[str] = None,
    status: Optional[str] = None,
    doc_id: Optional[str] = None,
    only_failed: bool = False,
    limit: int = 100,
):
    """Aggregate last records per doc_id with optional filters."""
    items: Dict[str, dict] = {}
    if INTEGRITY_REPORT_PATH.exists():
        # read from end for efficiency
        lines = INTEGRITY_REPORT_PATH.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            did = rec.get("doc_id")
            if not did or did in items:
                continue
            if matter_id and rec.get("matter_id") != matter_id:
                continue
            if status and rec.get("status") != status:
                continue
            if only_failed and not (
                ("result" in rec and not rec["result"].get("match")) or ("error" in rec)
            ):
                continue
            items[did] = rec
            if len(items) >= limit:
                break
    # Return as list sorted by ts desc
    result = sorted(items.values(), key=lambda x: x.get("ts", ""), reverse=True)
    return {"count": len(result), "items": result}


# --- B3: Mini-ACL (client token) ---
def _is_local_request(req: Request) -> bool:
    host = (req.client.host if req.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _enforce_client_token(req: Request, row: Doc | None = None) -> None:
    if not CLIENT_READ_TOKEN:
        return
    if _is_local_request(req):
        return
    if row is not None and getattr(row, "status", None) == "archive":
        return
    token = req.headers.get("X-Client-Token", "")
    if token != CLIENT_READ_TOKEN:
        raise HTTPException(status_code=401, detail="Missing or invalid client token")

def ensure_matter_structure(db: Session, matter_id: str) -> str:
    """Ensure /Matters/{YEAR}/{MatterID}/01_Intake exists, return folder id for 01_Intake."""
    if not GDRIVE_ROOT_FOLDER_ID:
        raise HTTPException(status_code=500, detail="GDRIVE_ROOT_FOLDER_ID not configured")
    root_id = GDRIVE_ROOT_FOLDER_ID
    root_name = GDRIVE_ROOT_PATH.strip("/").split("/")[0]

    year = matter_id.split("-")[0]
    matters_id = gdrive.find_or_create_folder(root_name, root_id)
    year_id = gdrive.find_or_create_folder(year, matters_id)
    matter_folder_id = gdrive.find_or_create_folder(matter_id, year_id)

    # Save matter record if not exists
    exists = db.execute(select(Matter).where(Matter.matter_id == matter_id)).scalar_one_or_none()
    if not exists:
        db.add(Matter(matter_id=matter_id, folder_path=f"/{root_name}/{year}/{matter_id}/"))
        db.commit()

    # Subfolders
    subs = [
        "01_Intake",
        "02_Evidence",
        "03_Pleadings",
        "04_Correspondence",
        "05_Court",
        "99_Archive",
        "Client_Share",
    ]
    sub_ids = {name: gdrive.find_or_create_folder(name, matter_folder_id) for name in subs}
    return sub_ids["01_Intake"]


# --- Notifications (file log + optional Email/Matrix) ---
def notify(event: str, payload: dict) -> None:
    if not NOTIF_ENABLE:
        return
    try:
        log_path = Path(NOTIF_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": event,
            "payload": payload,
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Email hook: doc_registered/result_delivered
        if (
            event in ("doc_registered", "result_delivered")
            and SMTP_HOST and SMTP_PORT and EMAIL_FROM and EMAIL_TO is not None
        ):
            subj_prefix = "Registered" if event == "doc_registered" else "Result delivered"
            subject = f"[Consilium] {subj_prefix} {payload.get('doc_id','')}"
            body = (
                ("Event: " + event + "\n") +
                f"Matter: {payload.get('matter_id','')}\n" +
                f"Title: {payload.get('title','')}\n" +
                f"DocID: {payload.get('doc_id','')}\n" +
                f"Link:  {payload.get('permalink','')}\n"
            )
            err = send_email(
                host=SMTP_HOST,
                port=SMTP_PORT or 587,
                user=SMTP_USER or "",
                password=SMTP_PASS or "",
                sender=EMAIL_FROM,
                recipient=EMAIL_TO,
                subject=subject,
                body=body,
            )
            if err:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": entry["ts"], "event": "email_error", "error": err}, ensure_ascii=False) + "\n")
            else:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": entry["ts"], "event": "email_sent", "payload": {"event": event, "doc_id": payload.get("doc_id")}}, ensure_ascii=False) + "\n")

        # Matrix hook: doc_registered/result_delivered
        if (
            event in ("doc_registered", "result_delivered")
            and MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN and MATRIX_ROOM_ID
        ):
            title_line = "Doc registered" if event == "doc_registered" else "Result delivered"
            text = (
                f"{title_line}\n"
                f"Matter: {payload.get('matter_id','')}\n"
                f"Title: {payload.get('title','')}\n"
                f"DocID: {payload.get('doc_id','')}\n"
                f"Link:  {payload.get('permalink','')}\n"
            )
            merr = send_matrix_message(
                homeserver=MATRIX_HOMESERVER,
                access_token=MATRIX_ACCESS_TOKEN,
                room_id=MATRIX_ROOM_ID,
                text=text,
            )
            if merr:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": entry["ts"], "event": "matrix_error", "error": merr}, ensure_ascii=False) + "\n")
            else:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": entry["ts"], "event": "matrix_sent", "payload": {"event": event, "doc_id": payload.get("doc_id")}}, ensure_ascii=False) + "\n")
    except Exception:
        # не роняем запрос из-за уведомлений
        pass


@app.post("/api/docs/register")
async def register_document(
    matter_id: str = Form(...),
    class_: str = Form(..., alias="class"),
    title: str = Form(...),
    file: UploadFile = File(...),
    origin: str | None = Form(default=None),
    origin_meta: str | None = Form(default=None),  # JSON string
    owner: str | None = Form(default=None),
    status: str | None = Form(default=None),
    tags: str | None = Form(default=None),  # JSON array string
):
    # Save upload to temp file to hash
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        temp_path = tf.name
        total = 0
        max_bytes = UPLOAD_MAX_BYTES_AUDIO if _is_audio(file.filename, file.content_type) else UPLOAD_MAX_BYTES_DEFAULT
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tf.write(chunk)
            total += len(chunk)
            if total > max_bytes:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                return JSONResponse(status_code=413, content={"error": "File too large"})
    try:
        try:
            # MIME and extension validation (best-effort)
            ctype = (file.content_type or "").lower()
            _, req_ext = os.path.splitext(file.filename or "")
            req_ext = req_ext.lower()
            if ctype and ctype not in ALLOWED_CONTENT_TYPES:
                return JSONResponse(status_code=415, content={"error": f"Unsupported content type: {ctype}"})
            if req_ext and req_ext not in ALLOWED_EXTS:
                return JSONResponse(status_code=415, content={"error": f"Unsupported file extension: {req_ext}"})
            sha256 = compute_sha256(temp_path)
            doc_id = generate_doc_id()

            # Ensure folder and upload to Drive
            with SessionLocal() as db:
                intake_folder_id = ensure_matter_structure(db, matter_id)

            safe_name = f"{doc_id}__{title}"
            _, ext = os.path.splitext(file.filename or "")
            target_name = f"{safe_name}{ext}" if ext else safe_name
            uploaded = gdrive.upload_file(intake_folder_id, temp_path, target_name)
            storage_ref = uploaded.get("id")
            web_link = uploaded.get("webViewLink")

            permalink = build_permalink(doc_id)

            # Save DB record
            with SessionLocal() as db:
                db_doc = Doc(
                    doc_id=doc_id,
                    matter_id=matter_id,
                    class_name=class_,
                    title=title,
                    sha256_plain=sha256,
                    storage="gdrive",
                    storage_ref=storage_ref,
                    origin=(origin or "upload"),
                    origin_meta=(json.loads(origin_meta) if origin_meta else None),
                    owner=owner,
                    status=(status or "registered"),
                    tags=(json.loads(tags) if tags else None),
                )
                db.add(db_doc)
                db.commit()

            # уведомление о регистрации
            notify(
                "doc_registered",
                {
                    "matter_id": matter_id,
                    "class": class_,
                    "title": title,
                    "doc_id": doc_id,
                    "permalink": permalink,
                },
            )

            return JSONResponse(
                status_code=201,
                content={
                    "doc_id": doc_id,
                    "permalink": permalink,
                    "sha256": sha256,
                    "storage": "gdrive",
                    "storage_ref": storage_ref,
                    "webViewLink": web_link,
                },
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass


@app.post("/api/hooks/docassemble")
def hook_docassemble(payload: DocassembleHook, request: Request):
    # Token guard
    if DOCASSEMBLE_HOOK_TOKEN:
        token = request.headers.get("X-Hook-Token", "")
        if token != DOCASSEMBLE_HOOK_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid hook token")

    # Validate input
    if not payload.matter_id or not payload.title:
        raise HTTPException(status_code=400, detail="matter_id and title are required")
    if not (payload.file_base64 or payload.file_url):
        raise HTTPException(status_code=400, detail="file_base64 or file_url required")

    temp_path = None
    try:
        # Prepare temp file
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            temp_path = tf.name
            if payload.file_base64:
                try:
                    data = base64.b64decode(payload.file_base64, validate=True)
                except Exception:
                    raise HTTPException(status_code=400, detail="Invalid base64")
                # size limit with audio consideration
                is_audio = _is_audio(payload.title, None)
                limit = UPLOAD_MAX_BYTES_AUDIO if is_audio else UPLOAD_MAX_BYTES_DEFAULT
                if len(data) > limit:
                    raise HTTPException(status_code=413, detail="File too large")
                tf.write(data)
            else:
                try:
                    r = requests.get(payload.file_url, timeout=20)
                    r.raise_for_status()
                    data = r.content
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"Failed to download: {e}")
                is_audio = _is_audio(payload.title, r.headers.get("Content-Type"))
                limit = UPLOAD_MAX_BYTES_AUDIO if is_audio else UPLOAD_MAX_BYTES_DEFAULT
                if len(data) > limit:
                    raise HTTPException(status_code=413, detail="File too large")
                tf.write(data)

        # Compute hash and upload like register_document
        sha256 = compute_sha256(temp_path)
        doc_id = generate_doc_id()

        with SessionLocal() as db:
            intake_folder_id = ensure_matter_structure(db, payload.matter_id)

        # Guess extension from title or content
        title = payload.title
        _, ext = os.path.splitext(title)
        if not ext:
            # try mimetype by sniffing
            mime = mimetypes.guess_type(title)[0]
            if mime:
                ext = mimetypes.guess_extension(mime) or ""
        safe_name = f"{doc_id}__{title}"
        target_name = f"{safe_name}{ext}" if ext else safe_name

        uploaded = gdrive.upload_file(intake_folder_id, temp_path, target_name)
        storage_ref = uploaded.get("id")
        web_link = uploaded.get("webViewLink")

        permalink = build_permalink(doc_id)

        # Save DB record
        with SessionLocal() as db:
            db_doc = Doc(
                doc_id=doc_id,
                matter_id=payload.matter_id,
                class_name=(payload.class_ or "generated"),
                title=title,
                sha256_plain=sha256,
                storage="gdrive",
                storage_ref=storage_ref,
                origin="docassemble",
                origin_meta=payload.origin_meta,
                owner=None,
                status="registered",
                tags=None,
            )
            db.add(db_doc)
            db.commit()

        # notify
        notify(
            "doc_registered",
            {
                "matter_id": payload.matter_id,
                "class": payload.class_ or "generated",
                "title": title,
                "doc_id": doc_id,
                "permalink": permalink,
            },
        )

        return JSONResponse(
            status_code=201,
            content={
                "doc_id": doc_id,
                "permalink": permalink,
                "sha256": sha256,
                "storage": "gdrive",
                "storage_ref": storage_ref,
                "webViewLink": web_link,
            },
        )
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass
@app.get("/doc/{doc_id}")
def resolve_doc(doc_id: str, request: Request):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        if row.storage != "gdrive" or not row.storage_ref:
            raise HTTPException(status_code=500, detail="Unsupported storage or missing ref")
        _enforce_client_token(request, row)
        url = f"https://drive.google.com/file/d/{row.storage_ref}/view?usp=drivesdk"
        return RedirectResponse(url)


@app.head("/doc/{doc_id}")
def resolve_doc_head(doc_id: str, request: Request):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        if row.storage != "gdrive" or not row.storage_ref:
            raise HTTPException(status_code=500, detail="Unsupported storage or missing ref")
        _enforce_client_token(request, row)
        url = f"https://drive.google.com/file/d/{row.storage_ref}/view?usp=drivesdk"
        # HEAD: отдаем только заголовки с Location, без тела
        return RedirectResponse(url)


@app.get("/api/docs/{doc_id}")
def get_doc(doc_id: str, request: Request):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        _enforce_client_token(request, row)
        return {
            "doc_id": row.doc_id,
            "matter_id": row.matter_id,
            "class": row.class_name,
            "title": row.title,
            "sha256": row.sha256_plain,
            "storage": row.storage,
            "storage_ref": row.storage_ref,
            "origin": row.origin,
            "origin_meta": row.origin_meta,
            "owner": row.owner,
            "status": row.status,
            "tags": row.tags,
        }


@app.post("/api/docs/{doc_id}/deliver")
def deliver_doc(doc_id: str, message: str | None = Form(default=None)):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        if row.storage != "gdrive" or not row.storage_ref:
            raise HTTPException(status_code=500, detail="Unsupported storage or missing ref")

        # Keep needed fields before session closes to avoid DetachedInstanceError
        loc_doc_id = row.doc_id
        loc_matter_id = row.matter_id
        loc_title = row.title
        loc_storage_ref = row.storage_ref
        loc_sha256 = row.sha256_plain or ""

        # Optional embedding on deliver (revision mode by default)
        if EMBED_ON_DELIVER and EMBED_MODE == "revision":
            try:
                # 1) Download current content
                content = gdrive.download_file_content(loc_storage_ref)
                # Try to infer extension from Drive name
                try:
                    nm = gdrive.get_file_name_mime(loc_storage_ref).get("name", "")
                    _, ext = os.path.splitext(nm)
                    ext = ext or ""
                except Exception:
                    ext = ""

                with tempfile.TemporaryDirectory() as td:
                    in_path = Path(td) / ("input" + ext)
                    with open(in_path, "wb") as f:
                        f.write(content)

                    # 2) Run embed_metadata.py to produce with_meta file
                    cmd = [
                        sys.executable,
                        str(Path(__file__).resolve().parents[1] / "scripts" / "embed_metadata.py"),
                        "--file", str(in_path),
                        "--doc-id", loc_doc_id,
                        "--matter-id", loc_matter_id,
                        "--sha256", loc_sha256,
                        "--title", loc_title or "",
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        raise RuntimeError(f"embed_metadata failed: {result.stderr.strip()}")
                    out_path = Path(result.stdout.strip())
                    if not out_path.exists():
                        raise RuntimeError("embed_metadata produced no file")

                    # 3) Upload as new version of the same Drive file
                    gdrive.update_file_content(loc_storage_ref, str(out_path))

                    # 4) Recompute sha256 and update DB
                    try:
                        new_sha = compute_sha256(str(out_path))
                    except Exception:
                        # fallback: hash freshly downloaded content from Drive
                        try:
                            dl = gdrive.download_file_content(loc_storage_ref)
                            import hashlib
                            h = hashlib.sha256(); h.update(dl)
                            new_sha = h.hexdigest()
                        except Exception as e:
                            print(f"[deliver] failed to compute new sha256: {e}")
                            new_sha = None
                    if new_sha:
                        row.sha256_plain = new_sha
                        row.updated_at = datetime.utcnow()
                        db.add(row)
                        db.commit()
                        loc_sha256 = new_sha
            except Exception as e:
                # не блокируем выдачу; просто продолжаем без вшивки
                print(f"[deliver] embed/update skipped due to error: {e}")

        # Final sync: ensure DB sha256 matches current Drive content
        try:
            dl = gdrive.download_file_content(loc_storage_ref)
            import hashlib
            h = hashlib.sha256(); h.update(dl)
            current_sha = h.hexdigest()
            if current_sha and current_sha != (row.sha256_plain or ""):
                row.sha256_plain = current_sha
                row.updated_at = datetime.utcnow()
                db.add(row)
                db.commit()
                loc_sha256 = current_sha
        except Exception as e:
            print(f"[deliver] sha sync skipped: {e}")

    permalink = build_permalink(loc_doc_id)
    payload = {
        "matter_id": loc_matter_id,
        "title": loc_title,
        "doc_id": loc_doc_id,
        "permalink": permalink,
    }
    if message:
        payload["message"] = message
    notify("result_delivered", payload)
    return {"ok": True, "doc_id": loc_doc_id, "permalink": permalink}


@app.patch("/api/docs/{doc_id}")
def patch_doc(doc_id: str, payload: PatchDoc):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        if payload.title is not None:
            row.title = payload.title
        if payload.storage is not None:
            row.storage = payload.storage
        if payload.storage_ref is not None:
            row.storage_ref = payload.storage_ref
        if payload.status is not None:
            row.status = payload.status
        if payload.tags is not None:
            row.tags = payload.tags
        if payload.owner is not None:
            row.owner = payload.owner
        if payload.origin is not None:
            row.origin = payload.origin
        if payload.origin_meta is not None:
            row.origin_meta = payload.origin_meta
        db.add(row)
        db.commit()
        return {"ok": True}


@app.post("/api/docs/{doc_id}/verify")
def verify_doc(doc_id: str):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        if row.storage != "gdrive" or not row.storage_ref:
            raise HTTPException(status_code=500, detail="Unsupported storage or missing ref")
        content = gdrive.download_file_content(row.storage_ref)
        import hashlib
        h = hashlib.sha256()
        h.update(content)
        current = h.hexdigest()
        return {"doc_id": doc_id, "sha256_current": current, "sha256_stored": row.sha256_plain, "match": current == row.sha256_plain}


@app.post("/api/docs/{doc_id}/sync_sha")
def sync_doc_sha(doc_id: str):
    """Пересчитать SHA256 по текущему содержимому на Drive и сохранить в БД."""
    try:
        with SessionLocal() as db:
            row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Doc not found"})
            if row.storage != "gdrive" or not row.storage_ref:
                return JSONResponse(status_code=500, content={"error": "Unsupported storage or missing ref"})
            prev = row.sha256_plain
            content = gdrive.download_file_content(row.storage_ref)
            import hashlib
            h = hashlib.sha256(); h.update(content)
            current = h.hexdigest()
            changed = current != prev
            if changed:
                row.sha256_plain = current
                row.updated_at = datetime.utcnow()
                db.add(row)
                db.commit()
            return {"doc_id": doc_id, "sha256_previous": prev, "sha256_updated": current, "changed": changed}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- B4: Admin view ---
@app.get("/admin/docs")
def admin_docs(
    request: Request,
    limit: int = 200,  # deprecated, kept for backward compat
    matter_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
):
    page = max(1, page)
    per_page = max(1, min(200, per_page))
    with SessionLocal() as db:
        q = select(Doc)
        if matter_id:
            q = q.where(Doc.matter_id == matter_id)
        if status:
            q = q.where(Doc.status == status)
        # order by updated_at desc (NULLS LAST) then doc_id desc
        try:
            q = q.order_by(desc(Doc.updated_at), desc(Doc.doc_id))
        except Exception:
            pass
        offset = (page - 1) * per_page
        q = q.offset(offset).limit(per_page)
        rows = db.execute(q).scalars().all()
    # pagination hints
    has_next = len(rows) == per_page
    has_prev = page > 1
    return templates.TemplateResponse(
        "admin/docs.html",
        {
            "request": request,
            "docs": rows,
            "msg": request.query_params.get("msg", ""),
            "filters": {"matter_id": matter_id or "", "status": status or ""},
            "statuses": ["draft", "submitted", "triage", "registered"],
            "page": page,
            "per_page": per_page,
            "has_next": has_next,
            "has_prev": has_prev,
        },
    )


@app.post("/admin/docs/{doc_id}/verify")
def admin_docs_verify(doc_id: str):
    # выполнить проверку и показать результат в баннере
    msg = ""
    try:
        res = verify_doc(doc_id)
        ok = res.get("match")
        msg = f"Verify {doc_id}: match={ok}"
    except Exception as e:
        msg = f"Verify {doc_id}: error: {e}"
    return RedirectResponse(url=f"/admin/docs?msg={msg}", status_code=303)


# --- C1.4: Admin status transitions ---
@app.post("/admin/docs/{doc_id}/status")
def admin_docs_set_status(doc_id: str, target: str = Form(...)):
    allowed = {"draft", "submitted", "triage", "registered"}
    if target not in allowed:
        return RedirectResponse(url=f"/admin/docs?msg=Unknown+status", status_code=303)
    msg = ""
    try:
        with SessionLocal() as db:
            row = db.get(Doc, doc_id)
            if not row:
                return RedirectResponse(url=f"/admin/docs?msg=Not+found", status_code=303)
            row.status = target
            row.updated_at = datetime.utcnow()
            db.add(row)
            db.commit()
        msg = f"Status {doc_id} → {target}"
    except Exception as e:
        msg = f"Status {doc_id} error: {e}"
    return RedirectResponse(url=f"/admin/docs?msg={msg}", status_code=303)


# --- C1: Intake UI (basic form) ---
@app.get("/intake")
def intake_form(request: Request):
    return templates.TemplateResponse("intake/form.html", {"request": request})


@app.post("/admin/docs/{doc_id}/sync_sha")
def admin_docs_sync_sha(doc_id: str):
    msg = ""
    try:
        res = sync_doc_sha(doc_id)
        if isinstance(res, JSONResponse):
            # unwrap JSONResponse content for message
            msg = f"Sync {doc_id}: status={res.status_code}"
        else:
            changed = res.get("changed")
            msg = f"Sync {doc_id}: changed={changed}"
    except Exception as e:
        msg = f"Sync {doc_id}: error: {e}"
    return RedirectResponse(url=f"/admin/docs?msg={msg}", status_code=303)
