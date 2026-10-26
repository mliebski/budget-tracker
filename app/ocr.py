"""Client for the local OCR container (Tesseract). Nothing leaves the home network.

OCR_URL is set by docker-compose (http://ocr:8080). Without it, e.g. in local
development, OCR is switched off and receipts are entered by hand.
"""
import json
import os
import time
import urllib.error
import urllib.request

from . import files

OCR_URL = os.environ.get("OCR_URL", "").rstrip("/")
TIMEOUT = 180


class OcrError(Exception):
    pass


def enabled() -> bool:
    return bool(OCR_URL)


STATUS_TIMEOUT = 2
FAILURE_TTL = 30  # seconds a failed check is remembered, so a down service doesn't slow every page load
_failed: dict[str, tuple[float, dict]] = {}


def status() -> dict:
    if not enabled():
        return {"ok": False, "detail": "not configured (OCR_URL is not set)"}
    cached = _failed.get(OCR_URL)
    if cached and time.monotonic() - cached[0] < FAILURE_TTL:
        return cached[1]
    try:
        with urllib.request.urlopen(f"{OCR_URL}/health", timeout=STATUS_TIMEOUT) as r:
            info = json.load(r)
    except (OSError, ValueError) as e:
        result = {"ok": False, "detail": f"not reachable ({e})"}
        _failed[OCR_URL] = (time.monotonic(), result)
        return result
    _failed.pop(OCR_URL, None)
    return {"ok": True, "detail": f"Tesseract {info.get('version', '')}, languages: "
                                  f"{', '.join(info.get('languages', []))}"}


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
