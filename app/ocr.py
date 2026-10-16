"""Client for the local OCR container (Tesseract). Nothing leaves the home network.

OCR_URL is set by docker-compose (http://ocr:8080). Without it, e.g. in local
development, OCR is switched off and receipts are entered by hand.
"""
import json
import os
import urllib.error
import urllib.request

from . import files

OCR_URL = os.environ.get("OCR_URL", "").rstrip("/")
TIMEOUT = 180


class OcrError(Exception):
    pass


def enabled() -> bool:
    return bool(OCR_URL)


def status() -> dict:
    if not enabled():
        return {"ok": False, "detail": "not configured (OCR_URL is not set)"}
    try:
        with urllib.request.urlopen(f"{OCR_URL}/health", timeout=5) as r:
            info = json.load(r)
        return {"ok": True, "detail": f"Tesseract {info.get('version', '')}, languages: "
                                      f"{', '.join(info.get('languages', []))}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "detail": f"not reachable ({e})"}


def read_text(stored_name: str, content_type: str) -> str:
    """Return the text of a stored photo or PDF. Raises OcrError."""
    if not enabled():
        raise OcrError("OCR is not configured.")
    if content_type == "application/pdf":
        body, ctype = files.path_of(stored_name).read_bytes(), "application/pdf"
    else:
        body, ctype = files.ocr_image(stored_name), "image/png"
    req = urllib.request.Request(f"{OCR_URL}/ocr", data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r).get("text", "")
    except urllib.error.HTTPError as e:
        raise OcrError(f"OCR failed: {e.read().decode(errors='replace')[:200]}")
    except (OSError, ValueError) as e:
        raise OcrError(f"OCR service not reachable: {e}")
