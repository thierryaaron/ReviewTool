"""Turn a manuscript file into what the API needs and what the quote check needs.

A PDF is sent to the model as a PDF, so the reviewer sees figures, tables and
page numbers. A Word file is converted to PDF with LibreOffice when it is
installed. Without LibreOffice, and for plain text or Markdown, the manuscript
is sent as text and the reviewer cannot see figures or page numbers.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_PDF_BYTES = 32 * 1024 * 1024  # API request limit
MAX_PDF_PAGES = 600


class ExtractError(Exception):
    pass


@dataclass
class Manuscript:
    name: str
    blocks: list[dict]  # content blocks for the API, the last one carries the cache marker
    text: str  # plain text, used only to check that quoted passages exist
    has_pages: bool
    pdf_path: Path | None = None


def load(path: Path, workdir: Path) -> Manuscript:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _from_pdf(path, path.name)
    if suffix in {".docx", ".doc", ".odt", ".rtf"}:
        pdf = _to_pdf(path, workdir)
        if pdf is not None:
            return _from_pdf(pdf, path.name)
        if suffix == ".docx":
            return _from_text(_docx_text(path), path.name)
        raise ExtractError(
            f"{path.name}: install LibreOffice to review {suffix} files, or save the file as PDF."
        )
    if suffix in {".txt", ".md", ".tex"}:
        return _from_text(path.read_text(encoding="utf-8", errors="replace"), path.name)
    raise ExtractError(f"{path.name}: unsupported file type {suffix or '(none)'}.")


def _from_pdf(pdf: Path, name: str) -> Manuscript:
    from pypdf import PdfReader

    data = pdf.read_bytes()
    if len(data) > MAX_PDF_BYTES:
        raise ExtractError(f"{name}: the PDF is larger than 32 MB, which the API will not accept.")
    reader = PdfReader(str(pdf))
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ExtractError(f"{name}: the PDF has more than {MAX_PDF_PAGES} pages.")
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    block = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
        "title": name,
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    return Manuscript(name=name, blocks=[block], text=text, has_pages=True, pdf_path=pdf)


def _from_text(text: str, name: str) -> Manuscript:
    if not text.strip():
        raise ExtractError(f"{name}: no text could be read from the file.")
    block = {
        "type": "text",
        "text": f'<manuscript filename="{name}">\n{text}\n</manuscript>',
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    return Manuscript(name=name, blocks=[block], text=text, has_pages=False)


def _to_pdf(path: Path, workdir: Path) -> Path | None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        return None
    workdir.mkdir(parents=True, exist_ok=True)
    # A private profile directory lets the conversion run while LibreOffice is open elsewhere.
    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation=file://{profile}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(workdir),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    pdf = workdir / (path.stem + ".pdf")
    if result.returncode != 0 or not pdf.exists():
        return None
    return pdf


def _docx_text(path: Path) -> str:
    try:
        import docx
    except ImportError as exc:
        raise ExtractError(
            "Reading .docx without LibreOffice needs python-docx (pip install python-docx)."
        ) from exc
    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class QuoteChecker:
    """Checks that a passage a reviewer quoted exists in the manuscript text.

    The comparison ignores case, spacing and punctuation, because text taken
    from a PDF rarely matches the page character for character.
    """

    def __init__(self, text: str):
        self._haystack = _squash(text)

    def found(self, quote: str) -> bool | None:
        needle = _squash(quote)
        if len(needle) < 12:
            return None  # too short to say anything
        if needle in self._haystack:
            return True
        # Reviewers shorten quotes with ellipses, so accept any long fragment.
        fragments = [_squash(f) for f in re.split(r"\.\.\.|…|\[[^\]]*\]", quote)]
        fragments = [f for f in fragments if len(f) >= 20]
        if fragments and all(f in self._haystack for f in fragments):
            return True
        return False
