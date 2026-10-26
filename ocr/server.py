"""OCR service for the budget app.

POST /ocr   body = a PNG/JPEG image or a PDF (Content-Type says which)
            -> {"text": "..."}
GET /health -> {"ok": true, "version": "...", "languages": [...]}

Uses only the Python standard library plus the tesseract and poppler command
line tools. PDFs with a text layer are read directly (fast); scanned PDFs are
rendered page by page and run through Tesseract.
"""
import functools
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LANGS = os.environ.get("OCR_LANGS", "eng+fra")
PSM = os.environ.get("OCR_PSM", "4")          # 4 = single column of text of variable sizes (receipts)
MAX_BYTES = 30 * 1024 * 1024
MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "5"))
TIMEOUT = 150
BUSY = threading.BoundedSemaphore(2)          # don't let several uploads starve a small box


def run(*args: str, both: bool = False) -> str:
    done = subprocess.run(args, capture_output=True, text=True, check=True, timeout=TIMEOUT)
    return done.stdout + done.stderr if both else done.stdout


def tesseract(image: Path) -> str:
    return run("tesseract", str(image), "stdout", "-l", LANGS, "--psm", PSM)


def read_pdf(path: Path, work: Path) -> str:
    text = run("pdftotext", "-layout", "-l", str(MAX_PAGES), str(path), "-")
    if len(text.strip()) >= 20:
        return text
    run("pdftoppm", "-r", "300", "-gray", "-png", "-l", str(MAX_PAGES), str(path), str(work / "page"))
    return "\n".join(tesseract(p) for p in sorted(work.glob("page*.png")))


@functools.cache  # the installed Tesseract doesn't change while the container runs
def version() -> str:
    first = run("tesseract", "--version", both=True).splitlines()
    return first[0].split()[-1] if first else ""


@functools.cache
def languages() -> list[str]:
    langs = run("tesseract", "--list-langs", both=True).splitlines()[1:]  # first line is a heading
    return [x.strip() for x in langs if x.strip() and x.strip() != "osd"]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        self._send(200, {"ok": True, "version": version(), "languages": languages()})

    def do_POST(self):
        if self.path != "/ocr":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BYTES:
            return self._send(413, {"error": "file missing or too large"})
        data = self.rfile.read(length)
        is_pdf = data[:5] == b"%PDF-"
        with BUSY, tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            src = work / ("in.pdf" if is_pdf else "in.img")
            src.write_bytes(data)
            try:
                text = read_pdf(src, work) if is_pdf else tesseract(src)
            except subprocess.CalledProcessError as e:
                return self._send(422, {"error": (e.stderr or str(e)).strip()[:300]})
            except subprocess.TimeoutExpired:
                return self._send(504, {"error": "OCR took too long"})
        self._send(200, {"text": text})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"OCR service on :8080 (Tesseract {version()}, languages {LANGS})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
