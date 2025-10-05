#!/usr/bin/env python3
"""
Embed Consilium metadata into PDF, DOCX, ODT, RTF.

Usage:
  python3 scripts/embed_metadata.py \
    --file path/to/input.ext \
    --doc-id D-2025... \
    --matter-id 2023-AR-0001 \
    --sha256 <hash> \
    [--title "Human Title"] \
    [--out-dir output/dir]

Output: writes <name>__with_meta.<ext> by default in the same folder (or --out-dir) and prints the path.

Note: This tool creates a copy with embedded metadata. Server-side integration can later implement
"revision" mode on Google Drive by uploading as the next file version using the produced content.
"""
import argparse
import json
import sys
from pathlib import Path

# Soft deps
try:
    from pypdf import PdfReader, PdfWriter
except Exception:
    PdfReader = None  # type: ignore
    PdfWriter = None  # type: ignore

try:
    import docx  # python-docx
except Exception:
    docx = None  # type: ignore

try:
    from odf.opendocument import load as odf_load, OpenDocumentText
    from odf import meta
except Exception:
    odf_load = None  # type: ignore
    OpenDocumentText = None  # type: ignore
    meta = None  # type: ignore


def build_keywords_json(doc_id: str, matter_id: str, sha256: str) -> str:
    return json.dumps({"DocID": doc_id, "MatterID": matter_id, "SHA256": sha256}, ensure_ascii=False)


def out_path_for(src: Path, out_dir: Path | None) -> Path:
    d = out_dir if out_dir else src.parent
    return d / f"{src.stem}__with_meta{src.suffix}"


def embed_pdf(src: Path, dst: Path, title: str | None, doc_id: str, matter_id: str, sha256: str) -> None:
    if PdfReader is None or PdfWriter is None:
        raise RuntimeError("pypdf is not installed")
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)
    info = reader.metadata or {}
    meta_dict = {
        "/Title": title or info.get("/Title", ""),
        "/Subject": "Consilium Resolver",
        "/Keywords": build_keywords_json(doc_id, matter_id, sha256),
        "/Producer": "Consilium",
    }
    writer.add_metadata(meta_dict)
    with dst.open("wb") as f:
        writer.write(f)


def embed_docx(src: Path, dst: Path, title: str | None, doc_id: str, matter_id: str, sha256: str) -> None:
    if docx is None:
        raise RuntimeError("python-docx is not installed")
    doc = docx.Document(str(src))
    cp = doc.core_properties
    if title:
        cp.title = title
    # Place structured data in keywords as JSON, human label in subject
    cp.subject = "Consilium Resolver"
    cp.keywords = build_keywords_json(doc_id, matter_id, sha256)
    doc.save(str(dst))


def embed_odt(src: Path, dst: Path, title: str | None, doc_id: str, matter_id: str, sha256: str) -> None:
    if odf_load is None or meta is None:
        raise RuntimeError("odfpy is not installed")
    doc = odf_load(str(src))
    # Use UserDefined meta fields for broad compatibility
    try:
        if title:
            doc.meta.addElement(meta.UserDefined(name="Title", valuetype="string", text=title))
        doc.meta.addElement(meta.UserDefined(name="Consilium", valuetype="string", text="Resolver"))
        doc.meta.addElement(
            meta.UserDefined(
                name="ConsiliumKeywords",
                valuetype="string",
                text=build_keywords_json(doc_id, matter_id, sha256),
            )
        )
    except Exception:
        # As a fallback, do not fail the whole operation; just proceed with saving
        pass
    doc.save(str(dst))


def embed_rtf(src: Path, dst: Path, title: str | None, doc_id: str, matter_id: str, sha256: str) -> None:
    # Minimalistic approach: ensure an \info group with \title and \doccomm JSON
    text = src.read_text(encoding="utf-8", errors="ignore")
    info_json = build_keywords_json(doc_id, matter_id, sha256)
    title_part = title or ""
    info_block = f"\\info\\title {title_part} \\doccomm {info_json} "
    if "\\info" in text:
        # naive replace: append our fields into existing info block
        new_text = text.replace("\\info", f"\\info {info_block}", 1)
    else:
        # insert at start after {\rtf...
        if text.startswith("{\\rtf"):
            brace = text.find(" ")
            if brace != -1:
                new_text = text[:brace] + " " + info_block + text[brace:]
            else:
                new_text = text + "{" + info_block + "}"
        else:
            new_text = "{\\rtf1 " + info_block + "}" + text
    dst.write_text(new_text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed Consilium metadata into common document formats")
    ap.add_argument("--file", required=True, help="Input file path (pdf|docx|odt|rtf)")
    ap.add_argument("--doc-id", required=True)
    ap.add_argument("--matter-id", required=True)
    ap.add_argument("--sha256", required=True)
    ap.add_argument("--title", default=None)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"Input not found: {src}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir) if args.out_dir else None
    dst = out_path_for(src, out_dir)

    ext = src.suffix.lower().lstrip(".")
    try:
        if ext == "pdf":
            embed_pdf(src, dst, args.title, args.doc_id, args.matter_id, args.sha256)
        elif ext == "docx":
            embed_docx(src, dst, args.title, args.doc_id, args.matter_id, args.sha256)
        elif ext == "odt":
            embed_odt(src, dst, args.title, args.doc_id, args.matter_id, args.sha256)
        elif ext == "rtf":
            embed_rtf(src, dst, args.title, args.doc_id, args.matter_id, args.sha256)
        else:
            print(f"Unsupported extension: .{ext}", file=sys.stderr)
            return 3
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 4

    print(str(dst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
