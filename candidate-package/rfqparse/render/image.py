"""Prepare an image (bytes) for a vision model: EXIF-rotate, flatten to RGB, downscale, re-encode.
An image that is already RGB (JPEG or PNG), upright and within the size cap is returned as the original bytes.

Needs Pillow (already pinned: pdfplumber depends on it). Never raises. Judges nothing about what the
picture is: no filename or inline-flag logic, and logos are the model's call. The only thing skipped is
an image too small to hold any content (a tracking pixel), and that skip is reported in `text`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from .. import config
from . import RenderResult

MAX_BYTES = 30_000_000


@dataclass
class ImageResult(RenderResult):
    data: bytes = b""            # the prepared image, empty unless status == "ok"
    mime: str | None = None      # "image/png" or "image/jpeg"
    width: int = 0               # prepared size
    height: int = 0
    orig_width: int = 0
    orig_height: int = 0
    orig_bytes: int = 0
    orig_format: str | None = None
    rotated: bool = False        # an EXIF orientation was applied
    downscaled: bool = False
    reencoded: bool = True       # False when the original bytes came back untouched

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


def _unreadable(reason: str, **kw) -> ImageResult:
    return ImageResult(status="unreadable", reason=reason, **kw)


def _sniff(data: bytes) -> str | None:
    """Name formats Pillow cannot decode, so the reason is specific."""
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"):
        return "HEIC/HEIF is not supported (no decoder installed); convert it to JPEG or PNG"
    if data[4:8] == b"ftyp" and data[8:12] == b"avif":
        return "AVIF is not supported (no decoder installed)"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower()):
        return "SVG vector image is not supported"
    if data[:5] == b"%PDF-":
        return "this is a PDF, not an image"
    return None


def prepare_image(data: bytes) -> ImageResult:
    try:
        return _prepare(data)
    except Exception as exc:  # contract: never raise
        return _unreadable(f"unexpected error: {type(exc).__name__}: {exc}")


def _flatten(img: Image.Image) -> Image.Image:
    """RGB with transparency composited onto white (a plain convert would turn it black)."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    if img.mode in ("I;16", "I;16L", "I;16B", "I"):
        img = img.point(lambda v: v / 256).convert("L") if img.mode.startswith("I;16") else img.convert("L")
    return img.convert("RGB")


def _encode(img: Image.Image) -> tuple[bytes, str]:
    """PNG keeps screenshots and text crisp; JPEG only wins when the picture is photographic
    (it must be less than half the PNG's size)."""
    png = io.BytesIO()
    img.save(png, format="PNG", optimize=True)
    jpg = io.BytesIO()
    img.save(jpg, format="JPEG", quality=config.IMAGE_JPEG_QUALITY)
    if len(jpg.getbuffer()) * 2 < len(png.getbuffer()):
        return jpg.getvalue(), "image/jpeg"
    return png.getvalue(), "image/png"


def _prepare(data: bytes) -> ImageResult:
    if not data:
        return _unreadable("empty file (0 bytes)")
    if len(data) > MAX_BYTES:
        return _unreadable(f"file too large ({len(data)} bytes, limit {MAX_BYTES})", orig_bytes=len(data))
    named = _sniff(data)
    if named:
        return _unreadable(named, orig_bytes=len(data))
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        w0, h0 = img.size
    except Exception as exc:
        return _unreadable(f"unsupported or corrupt image ({type(exc).__name__}: {exc})", orig_bytes=len(data))
    base = dict(orig_width=w0, orig_height=h0, orig_bytes=len(data), orig_format=fmt)
    if w0 * h0 > config.IMAGE_MAX_PIXELS:
        return _unreadable(f"image too large to decode safely ({w0}x{h0} pixels)", **base)
    if min(w0, h0) < config.IMAGE_MIN_EDGE:
        note = (f"[IMAGE SKIPPED: {w0}x{h0} px, a side is under {config.IMAGE_MIN_EDGE} px, too small to hold "
                f"content (tracking pixel or spacer)]")
        return ImageResult(status="skipped", text=note, reason=note, width=w0, height=h0, **base)
    try:
        img.seek(0)               # animated/multi-page: first frame only
        img.load()                # decode now so truncation surfaces here
        src_mode, frames = img.mode, getattr(img, "n_frames", 1)   # before any conversion
        try:
            rotated = img.getexif().get(0x0112, 1) not in (0, 1)   # EXIF orientation tag
        except Exception:
            rotated = False
        img = ImageOps.exif_transpose(img)   # returns a copy even when nothing needs rotating
        img = _flatten(img)
    except Exception as exc:
        return _unreadable(f"corrupt or truncated image ({type(exc).__name__}: {exc})", **base)

    if (fmt in ("JPEG", "PNG") and src_mode == "RGB" and not rotated and max(img.size) <= config.IMAGE_MAX_EDGE
            and frames == 1):
        mime = "image/jpeg" if fmt == "JPEG" else "image/png"   # already RGB, upright, small enough: send as is
        return ImageResult(status="ok", text=f"[image {w0}x{h0} {fmt.lower()}; unchanged]", data=data, mime=mime,
                           width=w0, height=h0, rotated=False, downscaled=False, reencoded=False, **base)

    downscaled = False
    longest = max(img.size)
    if longest > config.IMAGE_MAX_EDGE:
        scale = config.IMAGE_MAX_EDGE / longest
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
        downscaled = True
    out, mime = _encode(img)
    bits = [f"{w0}x{h0} {fmt or '?'}"]
    if rotated:
        bits.append("rotated upright by EXIF")
    if downscaled:
        bits.append(f"downscaled to {img.width}x{img.height}")
    note = f"[image {img.width}x{img.height} {mime.split('/')[1]}; from {', '.join(bits)}]"
    return ImageResult(status="ok", text=note, data=out, mime=mime, width=img.width, height=img.height,
                       rotated=rotated, downscaled=downscaled, **base)
