def _ensure_unique_docid_index() -> None:
    """Create UNIQUE index on docs.doc_id if missing (SQLite)."""
    with engine.connect() as conn:
        # Create unique index if not exists
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uidx_docs_doc_id ON docs(doc_id)"))
        conn.commit()

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
from sqlalchemy import select, desc, update
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
from sqlalchemy import text
import shutil

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
    OCR_LANGS,
    OCR_DPI,
    OCR_MAX_PAGES,
    DEBUG_OCR,
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

# --- C2.1: Lightweight Jobs table (for OCR queue) ---
def _init_jobs_table() -> None:
    with engine.connect() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                payload JSON,
                status TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        ))
        conn.commit()

_init_jobs_table()

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


# --- C2.1: OCR worker (skeleton) ---
def _enqueue_job(job_type: str, payload: dict) -> int:
    now = datetime.utcnow().isoformat() + "Z"
    with engine.connect() as conn:
        res = conn.execute(
            text("INSERT INTO jobs (type, payload, status, attempts, created_at, updated_at) VALUES (:t, :p, 'pending', 0, :c, :u)"),
            {"t": job_type, "p": json.dumps(payload, ensure_ascii=False), "c": now, "u": now},
        )
        conn.commit()
        return res.lastrowid if hasattr(res, "lastrowid") else 0


def _take_next_job(job_type: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, payload, attempts FROM jobs WHERE type=:t AND status='pending' ORDER BY id ASC LIMIT 1"),
            {"t": job_type},
        ).fetchone()
        if not row:
            return None
        jid = row[0]
        attempts = int(row[2] or 0)
        conn.execute(
            text("UPDATE jobs SET status='processing', attempts=:a, updated_at=:u WHERE id=:id"),
            {"a": attempts + 1, "u": datetime.utcnow().isoformat() + "Z", "id": jid},
        )
        conn.commit()
        try:
            payload = json.loads(row[1] or "{}")
        except Exception:
            payload = {}
        return {"id": jid, "payload": payload}


def _finish_job(job_id: int, status: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE jobs SET status=:s, updated_at=:u WHERE id=:id"),
            {"s": status, "u": datetime.utcnow().isoformat() + "Z", "id": job_id},
        )
        conn.commit()


async def _ocr_worker():
    while True:
        try:
            job = _take_next_job("ocr")
            if not job:
                await asyncio.sleep(2)
                continue
            jid = int(job["id"])
            payload = job.get("payload") or {}
            doc_id = payload.get("doc_id")
            mode = (payload.get("mode") or "auto").lower()
            try:
                print(f"[ocr] start jid={jid} payload={payload}")
            except Exception:
                pass
            if not doc_id:
                _finish_job(jid, "failed")
                continue
            # Real OCR: скачать файл из Drive, распознать, сохранить текст в origin_meta.ocr_text
            with SessionLocal() as db:
                row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
                if not row or not row.storage_ref:
                    _finish_job(jid, "failed")
                    continue
                # 1) Скачиваем содержимое
                try:
                    content = gdrive.download_file_content(row.storage_ref)
                except Exception:
                    _finish_job(jid, "failed")
                    continue
                # 2) Определяем тип по имени
                try:
                    nm = gdrive.get_file_name_mime(row.storage_ref).get("name", "")
                except Exception:
                    nm = row.title or "document"
                text_out, ocr_info = _run_ocr_pipeline(content, nm, mode)
                # 3) Тримминг длинных
                truncated = False
                max_len = 2 * 1024 * 1024  # 2MB
                if isinstance(text_out, bytes):
                    try:
                        text_out = text_out.decode("utf-8", errors="replace")
                    except Exception:
                        text_out = ""
                if len(text_out) > max_len:
                    text_out = text_out[:max_len] + "\n[truncated]"
                    truncated = True
                # 4) Сохраняем в origin_meta и теги
                meta = row.origin_meta or {}
                if isinstance(meta, list):
                    meta = {"note": "; ".join(str(x) for x in meta)}
                meta["ocr_text"] = text_out
                meta["ocr_info"] = {
                    "tool": ocr_info.get("tool"),
                    "code": ocr_info.get("code"),
                    "error": ocr_info.get("error"),
                    "truncated": truncated,
                    "mode": (ocr_info.get("mode") or mode),
                }
                if DEBUG_OCR:
                    try:
                        print(f"[ocr] save doc_id={doc_id} tool={meta['ocr_info']['tool']} mode={meta['ocr_info'].get('mode')} text_len={len(text_out)}")
                    except Exception:
                        pass
                row.origin_meta = meta
                tags = row.tags or []
                if isinstance(tags, dict):
                    tags = [f"k:{k}={v}" for k, v in tags.items()]
                tags = [t for t in tags if t != "ocr:queued"]
                if ocr_info.get("ok"):
                    if "ocr:done" not in tags:
                        tags.append("ocr:done")
                else:
                    if "ocr:failed" not in tags:
                        tags.append("ocr:failed")
                row.tags = tags
                row.updated_at = datetime.utcnow()
                db.add(row)
                # Also update any other rows with same doc_id (in case of duplicates)
                try:
                    db.execute(
                        update(Doc)
                        .where(Doc.doc_id == doc_id)
                        .values(origin_meta=meta, tags=tags, updated_at=datetime.utcnow())
                    )
                except Exception:
                    pass
                db.commit()
                _finish_job(jid, "done" if ocr_info.get("ok") else "failed")
        except Exception:
            # не роняем цикл воркера
            await asyncio.sleep(2)


@app.on_event("startup")
async def _startup_tasks():
    # fire-and-forget background task
    asyncio.create_task(_integrity_worker())
    asyncio.create_task(_ocr_worker())
    # ensure DB structures
    try:
        _init_jobs_table()
    except Exception:
        pass
    try:
        _ensure_unique_docid_index()
    except Exception:
        pass


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


# --- C2.1: enqueue OCR job for a document ---
@app.post("/api/ocr/enqueue")
def ocr_enqueue(doc_id: str = Form(...), mode: str = Form("auto")):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        # add tag marker and enqueue job
        tags = row.tags or []
        if isinstance(tags, dict):
            tags = [f"k:{k}={v}" for k, v in tags.items()]
        if "ocr:queued" not in tags:
            tags.append("ocr:queued")
        row.tags = tags
        row.updated_at = datetime.utcnow()
        db.add(row)
        db.commit()
    mode = (mode or "auto").lower()
    if mode not in ("auto", "image", "pdf"):
        mode = "auto"
    jid = _enqueue_job("ocr", {"doc_id": doc_id, "mode": mode})
    return {"ok": True, "job_id": jid}


# --- C2.2: OCR helpers ---
def _run_cmd(cmd: list[str], input_bytes: bytes | None = None, timeout_sec: int = 120) -> tuple[int, bytes, bytes]:
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input_bytes is not None else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(input=input_bytes, timeout=timeout_sec)
        return p.returncode, out or b"", err or b""
    except Exception as e:
        return 1, b"", str(e).encode()


def _run_ocr_pipeline(content: bytes, name: str, mode: str = "auto") -> tuple[str, dict]:
    name_lower = (name or "").lower()
    is_pdf = name_lower.endswith(".pdf") or (content[:4] == b"%PDF")
    if DEBUG_OCR:
        try:
            print(f"[ocr] detect: name={name_lower} is_pdf={is_pdf} mode={mode}")
        except Exception:
            pass
    # Try pdftotext for PDFs; if looks empty, fallback to tesseract per page
    if is_pdf:
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "input.pdf"
            with open(pdf_path, "wb") as f:
                f.write(content)
            # If forced image mode, skip pdftotext and go straight to tesseract
            if mode == "image":
                if DEBUG_OCR:
                    try:
                        print("[ocr] pipeline: mode=image -> skip pdftotext, using pdftoppm+tesseract")
                    except Exception:
                        pass
            elif shutil.which("pdftotext"):
                code, out, err = _run_cmd(["pdftotext", "-layout", str(pdf_path), "-"])
                text_pt = out.decode("utf-8", errors="replace") if out else ""
                # Heuristic: treat as empty if only whitespace/control OR no letters
                import re
                compact = re.sub(r"[\s\f\n\r]+", "", text_pt)
                has_letters = re.search(r"[A-Za-zА-Яа-я]", text_pt) is not None
                # additionally require letters within first 1000 chars
                head = text_pt[:1000]
                head_has_letters = re.search(r"[A-Za-zА-Яа-я]", head) is not None
                if code == 0 and len(compact) > 0 and has_letters and head_has_letters:
                    info = {"ok": True, "tool": "pdftotext", "code": code, "error": "", "mode": mode}
                    return text_pt, info
            # Fallback to images + tesseract if available
            if shutil.which("pdftoppm") and shutil.which("tesseract"):
                # Convert first N pages to PNG and OCR per page
                N = int(OCR_MAX_PAGES) if OCR_MAX_PAGES else 20
                # pdftoppm -r 300 -png input.pdf out
                code_ppm, _, err_ppm = _run_cmd([
                    "pdftoppm", "-r", str(int(OCR_DPI)), "-png",
                    "-f", "1", "-l", str(N),
                    str(pdf_path), str(Path(td)/"page")
                ], timeout_sec=600)
                if DEBUG_OCR:
                    try:
                        print(f"[ocr] pdftoppm: code={code_ppm} err={(err_ppm or b'').decode(errors='replace')[:200]}")
                    except Exception:
                        pass
                # Fallback to pdftocairo if pdftoppm failed
                if code_ppm != 0 and shutil.which("pdftocairo"):
                    code_ppm, _, err_ppm = _run_cmd([
                        "pdftocairo", "-png", "-r", str(int(OCR_DPI)),
                        str(pdf_path), str(Path(td)/"page"), "-f", "1", "-l", str(N)
                    ], timeout_sec=600)
                    if DEBUG_OCR:
                        try:
                            print(f"[ocr] pdftocairo: code={code_ppm} err={(err_ppm or b'').decode(errors='replace')[:200]}")
                        except Exception:
                            pass

                if code_ppm == 0:
                    pages = sorted(Path(td).glob("page-*.png"))[:N]
                    if DEBUG_OCR:
                        try:
                            print(f"[ocr] pages generated: {len(pages)} (cap={N})")
                        except Exception:
                            pass
                    texts: list[str] = []
                    for p in pages:
                        # Optional pre-processing with ImageMagick, if available
                        preprocessed = None
                        if shutil.which("convert"):
                            try:
                                preprocessed = p.with_name(p.stem + "-prep.png")
                                # Conservative pipeline: grayscale + normalize + slight sharpen
                                # Avoid aggressive thresholding to not lose fine glyphs
                                cmd_conv = [
                                    "convert", str(p),
                                    "-colorspace", "Gray",
                                    "-normalize",
                                    "-contrast-stretch", "0.5%x0.5%",
                                    "-sharpen", "0x1",
                                    str(preprocessed),
                                ]
                                cprep, _, eprep = _run_cmd(cmd_conv, timeout_sec=120)
                                if DEBUG_OCR:
                                    try:
                                        print(f"[ocr] preprocess convert page={p.name} code={cprep} err={(eprep or b'').decode(errors='replace')[:120]}")
                                    except Exception:
                                        pass
                                if cprep != 0:
                                    preprocessed = None
                            except Exception:
                                preprocessed = None

                        img_for_ocr = str(preprocessed or p)
                        c, o, e = _run_cmd(["tesseract", img_for_ocr, "-", "-l", OCR_LANGS, "--oem", "1", "--psm", "6"], timeout_sec=180)
                        if DEBUG_OCR and c != 0:
                            try:
                                print(f"[ocr] tesseract page={p.name} code={c} err={(e or b'').decode(errors='replace')[:200]}")
                            except Exception:
                                pass
                        if c == 0 and o:
                            texts.append(o.decode("utf-8", errors="replace"))
                        else:
                            # keep going; collect errors optionally
                            pass
                    txt = "\n\f\n".join(texts)
                    ok = len(txt.strip()) > 0
                    if DEBUG_OCR:
                        try:
                            print(f"[ocr] pipeline: using image fallback, pages={len(pages)}, ok={ok}")
                        except Exception:
                            pass
                    info = {"ok": ok, "tool": "pdftoppm+tesseract", "code": 0 if ok else 1, "error": (err_ppm.decode(errors="replace") if not ok else ""), "mode": mode}
                    return txt, info
            # If no fallback tools or still empty
            info = {"ok": False, "tool": "pdftotext" if mode != "image" else "pdftoppm+tesseract", "code": 1, "error": "empty_output", "mode": mode}
            return "", info
    # Try tesseract for images
    if shutil.which("tesseract") and (name_lower.endswith(".png") or name_lower.endswith(".jpg") or name_lower.endswith(".jpeg")):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / ("input" + (Path(name_lower).suffix or ".png"))
            with open(img_path, "wb") as f:
                f.write(content)
            code, out, err = _run_cmd(["tesseract", str(img_path), "-", "-l", OCR_LANGS])
            ok = code == 0 and len(out) > 0
            info = {"ok": ok, "tool": "tesseract", "code": code, "error": (err.decode(errors="replace") if code != 0 else ""), "mode": mode}
            return (out.decode("utf-8", errors="replace") if ok else ""), info
    # Fallback: no tool
    return "", {"ok": False, "tool": "none", "code": 127, "error": "No suitable OCR tool available", "mode": mode}


# --- C2.2: Read OCR text ---
@app.get("/api/docs/{doc_id}/text")
def get_doc_text(doc_id: str, request: Request):
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        _enforce_client_token(request, row)
        meta = row.origin_meta or {}
        text_val = ""
        truncated = False
        if isinstance(meta, dict):
            text_val = meta.get("ocr_text") or ""
            info = meta.get("ocr_info") or {}
            truncated = bool(info.get("truncated")) if isinstance(info, dict) else False
        return {"doc_id": row.doc_id, "text": text_val, "truncated": truncated}


# --- DEBUG: expose DB path and raw origin_meta ---
@app.get("/api/debug/db_path")
def debug_db_path():
    if not DEBUG_OCR:
        raise HTTPException(status_code=404, detail="Not Found")
    from .config import DB_PATH
    return {"db_path": DB_PATH}


@app.get("/api/debug/doc/{doc_id}/raw")
def debug_doc_raw(doc_id: str):
    if not DEBUG_OCR:
        raise HTTPException(status_code=404, detail="Not Found")
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
        return {
            "doc_id": row.doc_id,
            "origin_meta": row.origin_meta,
            "tags": row.tags,
            "updated_at": str(row.updated_at),
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


# --- Admin: list duplicate doc_ids
@app.get("/api/admin/docs/duplicates")
def admin_list_doc_duplicates():
    with engine.connect() as conn:
        rows = conn.execute(text(
            """
            SELECT doc_id, COUNT(*) as cnt
            FROM docs
            GROUP BY doc_id
            HAVING cnt > 1
            ORDER BY cnt DESC
            """
        )).fetchall()
        return {"duplicates": [{"doc_id": r[0], "count": r[1]} for r in rows]}


# --- Admin: re-OCR endpoints
@app.post("/api/admin/ocr/requeue")
def admin_requeue_ocr(doc_id: str, mode: str = "auto"):
    mode = (mode or "auto").lower()
    if mode not in ("auto", "image", "pdf"):
        mode = "auto"
    # ensure doc exists
    with SessionLocal() as db:
        row = db.execute(select(Doc).where(Doc.doc_id == doc_id)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Doc not found")
    jid = _enqueue_job("ocr", {"doc_id": doc_id, "mode": mode})
    return {"ok": True, "job_id": jid}


@app.post("/api/admin/ocr/requeue_batch")
def admin_requeue_ocr_batch(matter_id: str, mode: str = "auto"):
    mode = (mode or "auto").lower()
    if mode not in ("auto", "image", "pdf"):
        mode = "auto"
    count = 0
    with SessionLocal() as db:
        rows = db.execute(select(Doc.doc_id).where(Doc.matter_id == matter_id)).scalars().all()
        for did in rows:
            _enqueue_job("ocr", {"doc_id": did, "mode": mode})
            count += 1
    return {"ok": True, "enqueued": count, "mode": mode}


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
