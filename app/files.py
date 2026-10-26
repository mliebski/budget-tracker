"""Storing uploaded photos and PDFs on disk.

Files live below UPLOAD_DIR as <year>/<month>/<random>.<ext> and are never
modified after upload, so the nightly backup can simply copy new files.
Photos are re-encoded: rotated upright, shrunk to a sensible size and
stripped of metadata (phone photos carry GPS coordinates in EXIF).
"""
import io
import os
import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "./uploads"))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_SIDE = 3000      # stored photo, long side in px: plenty for reading a receipt
THUMB_SIDE = 480
MAX_PIXELS = 80_000_000        # PNG/WebP are decoded in full, so keep those modest
MAX_JPEG_PIXELS = 400_000_000  # JPEGs are decoded at reduced size (see draft below): 200 MP phones are fine
Image.MAX_IMAGE_PIXELS = None  # limits are checked by hand below, with a proper message


class UploadError(Exception):
    pass


@dataclass
class Stored:
    stored_name: str
    content_type: str
    size: int
    has_thumb: bool


def path_of(stored_name: str) -> Path:
    path = (UPLOAD_DIR / stored_name).resolve()
    if UPLOAD_DIR.resolve() not in path.parents:
        raise UploadError("Invalid file path.")
    return path


def thumb_name(stored_name: str) -> str:
    return stored_name.rsplit(".", 1)[0] + "_thumb.jpg"


def _new_name(ext: str) -> str:
    today = date.today()
    return f"{today:%Y}/{today:%m}/{secrets.token_hex(12)}.{ext}"


def _write(stored_name: str, data: bytes) -> None:
    path = path_of(stored_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def store(data: bytes) -> Stored:
    """Validate by content (not by the name or type the browser claims) and save."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"That file is too big (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB).")
    if not data:
        raise UploadError("The file is empty.")
    if data[:5] == b"%PDF-":
        name = _new_name("pdf")
        _write(name, data)
        return Stored(name, "application/pdf", len(data), False)

    try:
        img = Image.open(io.BytesIO(data))
        pixels = img.width * img.height
        if pixels > (MAX_JPEG_PIXELS if img.format == "JPEG" else MAX_PIXELS):
            raise UploadError(f"That photo is too large ({pixels // 1_000_000} megapixels). "
                              "Use the camera's normal resolution.")
        img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG only: decode at reduced size, saves memory
        img.load()
    except (UnidentifiedImageError, OSError):
        if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
            raise UploadError("HEIC photos aren't supported. On iPhone, set Camera › Formats to "
                              "“Most Compatible”, or take the photo from the upload button.")
        raise UploadError("Only photos (JPEG, PNG, WebP, …) and PDF files can be uploaded.")
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    name = _new_name("jpg")
    _write(name, out.getvalue())

    thumb = img.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    tout = io.BytesIO()
    thumb.save(tout, "JPEG", quality=75)
    _write(thumb_name(name), tout.getvalue())
    return Stored(name, "image/jpeg", out.tell(), True)


def remove(stored_name: str) -> None:
    for name in (stored_name, thumb_name(stored_name)):
        try:
            path_of(name).unlink(missing_ok=True)
        except UploadError:
            pass


def ocr_image(stored_name: str) -> bytes:
    """A version of a stored photo prepared for OCR: greyscale, sharp enough, PNG."""
    with Image.open(path_of(stored_name)) as photo:
        img = ImageOps.grayscale(photo)
    if max(img.size) < 1600:  # small photos read much better when enlarged
        factor = 1600 / max(img.size)
        img = img.resize((round(img.width * factor), round(img.height * factor)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def usage() -> dict:
    count = size = 0
    if UPLOAD_DIR.is_dir():
        for p in UPLOAD_DIR.rglob("*"):
            if p.is_file() and not p.name.endswith("_thumb.jpg"):
                count += 1
                size += p.stat().st_size
    return {"count": count, "size": size, "dir": str(UPLOAD_DIR)}
