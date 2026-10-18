"""Save an uploaded image or file onto a mind.

Images are scaled to a JPEG the vision model can read. Other files are stored
as themselves, with a size cap.
"""

import os

from images import prepare_for_model

MAX_ATTACHMENTS = 8
MAX_IMAGES = 4
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 48 * 1024 * 1024

_IMAGE_MAGIC = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"BM",
)


def looks_like_image(data: bytes) -> bool:
    if not data:
        return False
    for magic in _IMAGE_MAGIC:
        if data.startswith(magic):
            return True
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def store_upload(store, data: bytes, filename: str) -> dict:
    """Write one upload. Raises ValueError when it cannot be kept."""
    if not data:
        raise ValueError("empty file")
    name = filename or "file"
    if looks_like_image(data):
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("image is too large")
        try:
            jpeg, mime = prepare_for_model(data)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"could not read image: {e}") from e
        stem = os.path.splitext(os.path.basename(name))[0] or "image"
        return store.save_upload(jpeg, stem + ".jpg", kind="image", mime=mime)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file is larger than 32MB")
    import mimetypes
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return store.save_upload(data, name, kind="file", mime=mime)


def store_uploads(store, blobs: list[tuple[bytes, str]]) -> list[dict]:
    """Save several uploads, or none of them if one is rejected."""
    if len(blobs) > MAX_ATTACHMENTS:
        raise ValueError(f"at most {MAX_ATTACHMENTS} attachments")
    saved: list[dict] = []
    images = 0
    try:
        for data, name in blobs:
            rec = store_upload(store, data, name)
            if rec["kind"] == "image":
                images += 1
                if images > MAX_IMAGES:
                    _discard(store, rec)
                    raise ValueError(f"at most {MAX_IMAGES} images")
            saved.append(rec)
    except Exception:
        for rec in saved:
            _discard(store, rec)
        raise
    return saved


def discard_uploads(store, records: list[dict]) -> None:
    for rec in records or []:
        _discard(store, rec)


def _discard(store, rec: dict) -> None:
    path = (rec or {}).get("path") or ""
    if not path:
        return
    real = os.path.realpath(path)
    root = os.path.realpath(store.uploads_dir)
    if real == root or not real.startswith(root + os.sep):
        return
    try:
        os.remove(real)
    except OSError:
        pass
