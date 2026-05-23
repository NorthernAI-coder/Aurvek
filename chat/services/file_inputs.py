import base64
import io
import os

from PIL import Image as PilImage, ImageOps
from PIL.ExifTags import Base as ExifBase

from common import MAX_API_IMAGE_SIZE_MB, MAX_CHAT_IMAGE_DIMENSION, MAX_IMAGE_PIXELS
from log_config import logger


TEXT_FILE_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".css", ".sql", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".log", ".sh", ".bash",
    ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb",
    ".php", ".r", ".swift", ".kt", ".lua",
}


def is_text_file(content_type: str, filename: str) -> bool:
    """Check if a file is a recognized text file. Extension is the primary gate."""
    if not filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    if ext not in TEXT_FILE_EXTENSIONS:
        return False
    ct = (content_type or "").lower()
    mime_exceptions = {"video/mp2t"}
    if ct in mime_exceptions:
        return True
    if ct.startswith("image/") or ct == "application/pdf" or ct.startswith("audio/") or ct.startswith("video/"):
        return False
    return True


def decode_text_file(data: bytes, filename: str) -> str:
    """Decode text file bytes to UTF-8 string, handling common encodings."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except (UnicodeDecodeError, UnicodeError):
            raise ValueError(f"File '{filename}' has a UTF-16 BOM but could not be decoded")

    if b"\x00" in data[:8192]:
        raise ValueError(f"File '{filename}' appears to be a binary file")

    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    try:
        return data.decode("windows-1252")
    except UnicodeDecodeError:
        raise ValueError(f"File '{filename}' could not be decoded (unsupported encoding)")


def convert_to_jpeg_b64(image_data_b64: str) -> str:
    raw = base64.b64decode(image_data_b64)
    img = PilImage.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def maybe_compress_image(
    img: PilImage.Image, image_data: bytes, actual_format: str
) -> tuple[bytes, str, bool]:
    """Compress image to WebP q90 if beneficial. Sync, called via to_thread."""
    compress_formats = {"PNG", "BMP", "TIFF", "GIF"}
    size_threshold = 3 * 1024 * 1024

    if actual_format == "WEBP":
        return image_data, "image/webp", False

    should_compress = (
        actual_format in compress_formats
        or len(image_data) > size_threshold
    )

    if not should_compress:
        media_type = f"image/{actual_format.lower()}" if actual_format else "image/jpeg"
        return image_data, media_type, False

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA") if (
            img.mode in ("PA", "LA") or img.info.get("transparency") is not None
        ) else img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    compressed = buf.getvalue()

    if len(compressed) < len(image_data):
        return compressed, "image/webp", True

    media_type = f"image/{actual_format.lower()}" if actual_format else "image/jpeg"
    return image_data, media_type, False


SERVER_SHRINK_MAX_STEPS = 10


def encode_and_shrink_webp_to_fit(
    img: PilImage.Image, w: int, h: int, max_api_bytes: int, max_steps: int
) -> tuple[bytes, int, int]:
    base_img = img
    base_w, base_h = w, h

    buf = io.BytesIO()
    base_img.save(buf, format="WEBP", quality=90)
    image_data = buf.getvalue()

    shrink_step = 0
    while len(image_data) > max_api_bytes and shrink_step < max_steps:
        if base_w <= 1 or base_h <= 1:
            break
        shrink_step += 1
        ratio = 0.85 ** shrink_step
        target_w = max(1, round(base_w * ratio))
        target_h = max(1, round(base_h * ratio))
        attempt_img = base_img.resize((target_w, target_h), PilImage.LANCZOS)
        buf = io.BytesIO()
        attempt_img.save(buf, format="WEBP", quality=90)
        image_data = buf.getvalue()
        w, h = target_w, target_h
        logger.debug(
            "[encode_and_shrink_webp_to_fit] Shrink step %s: %sx%s, %s bytes",
            shrink_step,
            w,
            h,
            len(image_data),
        )

    return image_data, w, h


def validate_and_compress_image(
    image_data: bytes, filename: str
) -> tuple[bytes, str, int, int, str, bool]:
    """Open, validate, and optionally compress an image. Sync, called via to_thread."""
    try:
        img = PilImage.open(io.BytesIO(image_data))
        w, h = img.size
        actual_format = img.format
    except Exception:
        raise ValueError(f"Invalid image file: {filename}")

    if w * h > MAX_IMAGE_PIXELS:
        raise ValueError("Image resolution is too high.")

    try:
        orientation = img.getexif().get(ExifBase.Orientation, 1)
    except Exception:
        orientation = 1

    needs_reencode_for_exif = orientation != 1

    try:
        if needs_reencode_for_exif:
            img = ImageOps.exif_transpose(img)
            w, h = img.size
        else:
            img.load()
    except Exception:
        raise ValueError(f"Invalid image file: {filename}")

    resized_now = False
    if max(w, h) > MAX_CHAT_IMAGE_DIMENSION:
        ratio = MAX_CHAT_IMAGE_DIMENSION / max(w, h)
        new_w = max(1, round(w * ratio))
        new_h = max(1, round(h * ratio))
        logger.debug(
            "[validate_and_compress_image] Resizing %sx%s -> %sx%s",
            w,
            h,
            new_w,
            new_h,
        )
        img = img.resize((new_w, new_h), PilImage.LANCZOS)
        w, h = new_w, new_h
        resized_now = True

    max_api_bytes = MAX_API_IMAGE_SIZE_MB * 1024 * 1024

    if resized_now or needs_reencode_for_exif:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA") if (
                img.mode in ("PA", "LA") or img.info.get("transparency") is not None
            ) else img.convert("RGB")

        image_data, w, h = encode_and_shrink_webp_to_fit(
            img, w, h, max_api_bytes, SERVER_SHRINK_MAX_STEPS
        )
        return image_data, "image/webp", w, h, "WEBP", True

    image_data, media_type, was_compressed = maybe_compress_image(
        img, image_data, actual_format
    )

    if len(image_data) > max_api_bytes:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA") if (
                img.mode in ("PA", "LA") or img.info.get("transparency") is not None
            ) else img.convert("RGB")

        image_data, w, h = encode_and_shrink_webp_to_fit(
            img, w, h, max_api_bytes, SERVER_SHRINK_MAX_STEPS
        )
        return image_data, "image/webp", w, h, "WEBP", True

    return image_data, media_type, w, h, actual_format, was_compressed
