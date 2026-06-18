from __future__ import annotations

import csv
import io
import logging
import os

from sharepoint.graph_client import download_file_bytes, list_configured_sharepoint_drives, list_drive_items

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".csv", ".xlsx", ".pptx"}


def list_sharepoint_documents(max_items: int | None = None, max_depth: int = 8) -> list[dict]:
    documents: list[dict] = []
    for drive in list_configured_sharepoint_drives():
        remaining = None if max_items is None else max(max_items - len(documents), 0)
        if remaining == 0:
            break
        documents.extend(list_drive_items(drive["site_id"], drive["drive_id"], max_items=remaining, max_depth=max_depth))
    return documents


def download_sharepoint_document(item: dict) -> bytes:
    return download_file_bytes(item["drive_id"], item["id"])


def extract_document_text(file_name: str, file_bytes: bytes) -> str:
    ext = os.path.splitext(file_name or "")[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.info("Skipping unsupported SharePoint file type: %s", file_name)
        return ""
    if ext == ".txt":
        return file_bytes.decode("utf-8", errors="ignore")
    if ext == ".csv":
        text = file_bytes.decode("utf-8", errors="ignore")
        return "\n".join(", ".join(row) for row in csv.reader(io.StringIO(text)))
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext == ".xlsx":
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        lines: list[str] = []
        for sheet in workbook.worksheets:
            lines.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(cell) for cell in row if cell is not None]
                if values:
                    lines.append(", ".join(values))
        return "\n".join(lines)
    if ext == ".pptx":
        from pptx import Presentation

        presentation = Presentation(io.BytesIO(file_bytes))
        lines: list[str] = []
        for slide in presentation.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    lines.append(shape.text)
        return "\n".join(lines)
    return ""
