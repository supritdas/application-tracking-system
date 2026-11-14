import io
import re

from pypdf import PdfReader

MAX_PAGES = 15   # a resume longer than this is not a resume
MIN_CHARS = 50   # below this, the PDF is almost certainly a scan


class ExtractionError(Exception):
    """Permanent failure: retrying will not help."""


def extract_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("PDF is password-protected.")
        raw = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PAGES])
    except ExtractionError:
        raise
    except Exception as exc:  # pypdf raises many exception types on malformed files
        raise ExtractionError(f"Could not read PDF: {exc}") from exc

    # PostgreSQL text columns reject NUL bytes, and PDFs sometimes contain them.
    text = raw.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) < MIN_CHARS:
        raise ExtractionError("No selectable text found. The PDF may be a scanned image.")
    return text
