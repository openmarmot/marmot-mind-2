"""Queue a file or image onto the reply that ends this think loop."""

import json

from attachments import MAX_ATTACHMENTS, MAX_IMAGES, discard_uploads, store_upload
from .look_at_image import _clean_source, _load_image

_ATTACH_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "attach_file",
        "description": (
            "Attach an image or other file to the reply you are about to send. "
            "source is a workspace path, an absolute path, or an http(s) URL. "
            "Up to 4 images and 8 attachments. The reply text is your final message; "
            "an attachment with no text still sends."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Workspace path, absolute path, or http(s) URL.",
                },
            },
            "required": ["source"],
        },
    },
}


def execute_attach_file(ctx, source: str) -> str:
    src = _clean_source(source)
    if not src:
        return json.dumps({"status": "error", "message": "empty source"})
    if ctx is None or getattr(ctx, "store", None) is None:
        return json.dumps({"status": "error", "message": "chat unavailable"})
    outgoing = ctx.outgoing
    if len(outgoing) >= MAX_ATTACHMENTS:
        return json.dumps({
            "status": "error",
            "message": f"at most {MAX_ATTACHMENTS} attachments on a reply",
        })
    workspace = ctx.tool_calls_dir
    try:
        if _might_be_image(src):
            data, _mime, label = _load_image(src, workspace)
        else:
            data, _mime, label = _load_any(src, workspace)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e), "source": src})
    try:
        rec = store_upload(ctx.store, data, _name_from_label(label))
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e), "source": src})
    if rec["kind"] == "image":
        images = sum(1 for item in outgoing if item.get("kind") == "image")
        if images >= MAX_IMAGES:
            discard_uploads(ctx.store, [rec])
            return json.dumps({
                "status": "error",
                "message": f"at most {MAX_IMAGES} images on a reply",
            })
    outgoing.append(rec)
    return json.dumps({
        "status": "attached",
        "name": rec["name"],
        "kind": rec["kind"],
        "path": rec["path"],
        "message": "It will go out with your final reply.",
    }, ensure_ascii=False)


def _might_be_image(source: str) -> bool:
    low = source.lower().split("?", 1)[0]
    if low.startswith("data:image/"):
        return True
    return low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


def _load_any(source: str, workspace: str | None) -> tuple[bytes, str, str]:
    """Load a non-image file from disk. URLs that are not images are refused."""
    import os
    if source.startswith(("http://", "https://", "data:")):
        # Try the image loader anyway; a URL of a picture without a suffix still works.
        return _load_image(source, workspace)
    path = source[7:] if source.startswith("file://") else source
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(workspace or os.getcwd(), path)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError(f"not a file: {path}")
    size = os.path.getsize(path)
    if size == 0:
        raise ValueError("empty file")
    if size > 32 * 1024 * 1024:
        raise ValueError("file is larger than 32MB")
    with open(path, "rb") as f:
        data = f.read()
    return data, "application/octet-stream", path


def _name_from_label(label: str) -> str:
    import os
    if label.startswith(("http://", "https://")):
        return os.path.basename(label.split("?", 1)[0]) or "file"
    return os.path.basename(label) or "file"
