"""Turn a mind's own chat into a normal user/assistant transcript.

The model sees timestamps on every message. A scheduled wake with nothing new
from the human is a single [runtime] line, not a fake person.
"""

import base64
import os

from clock import format_local, utcnow
from images import prepare_for_model


def choose_transcript(recent: list, unseen_human: list, limit: int = 48) -> tuple[list, bool]:
    """Merge recent messages with unseen human ones, oldest first.

    Newer messages win when the window is short. Unseen human messages are
    kept ahead of older chatter, and if there are more of those than `limit`,
    the newest of them stay.
    """
    by_id = {}
    for src in (recent or [], unseen_human or []):
        for m in src:
            by_id[int(m["id"])] = m
    ordered = [by_id[i] for i in sorted(by_id)]
    if len(ordered) <= limit:
        return ordered, False
    unseen_ids = {int(m["id"]) for m in (unseen_human or [])}
    chosen = []
    for m in reversed(ordered):
        if int(m["id"]) in unseen_ids:
            chosen.append(m)
    for m in reversed(ordered):
        if int(m["id"]) not in unseen_ids:
            if len(chosen) >= limit:
                break
            chosen.append(m)
    if len(chosen) > limit:
        chosen = [m for m in chosen if int(m["id"]) in unseen_ids][:limit]
    chosen.sort(key=lambda m: int(m["id"]))
    return chosen, True


def render_for_model(msg: dict, human_name: str) -> str:
    """One timestamped message, plus a path for each attachment."""
    when = format_local(msg.get("created_at") or "")
    head = f"[{when}]" if when else "[time unknown]"
    if msg.get("author") == "human":
        head += f" {human_name}:"
    lines = [head]
    text = (msg.get("text") or "").strip()
    attachments = msg.get("attachments") or []
    if text:
        lines.append(text)
    elif not attachments:
        lines.append("(empty)")
    for att in attachments:
        if not isinstance(att, dict):
            continue
        kind = "image" if att.get("kind") == "image" else "file"
        name = att.get("name") or kind
        path = att.get("path") or ""
        lines.append(f"[attached {kind} {name}: {path}]")
    return "\n".join(lines)


def message_to_llm(msg: dict, *, vision: bool, human_name: str) -> dict:
    body = render_for_model(msg, human_name)
    role = "assistant" if msg.get("author") == "mind" else "user"
    if role != "user" or not vision:
        return {"role": role, "content": body}
    images = _image_parts(msg)
    if not images:
        return {"role": "user", "content": body}
    return {"role": "user", "content": [{"type": "text", "text": body}, *images]}


def runtime_wake_line(reason: str, now=None) -> str:
    why = (reason or "Scheduled wake").strip().rstrip(".")
    stamp = format_local(now or utcnow())
    return (
        f"[runtime {stamp}] {why}. "
        "The human has not sent a new message. This line is the clock, not a person."
    )


def build_llm_messages(
    *,
    system_prompt: str,
    notes: str,
    transcript: list,
    unseen_ids: set,
    human_name: str,
    wake_line: str | None,
) -> list:
    system = (system_prompt or "").strip()
    extra = (notes or "").strip()
    if extra:
        system = f"{system}\n\n{extra}" if system else extra
    out = [{"role": "system", "content": system}]
    unseen_ids = {int(i) for i in (unseen_ids or set())}
    for msg in transcript or []:
        vision = int(msg["id"]) in unseen_ids and msg.get("author") == "human"
        out.append(message_to_llm(msg, vision=vision, human_name=human_name))
    if wake_line:
        out.append({"role": "user", "content": wake_line})
    return out


def _image_parts(msg: dict) -> list:
    parts = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict) or att.get("kind") != "image":
            continue
        path = att.get("path") or ""
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
            data, mime = prepare_for_model(data)
        except Exception:
            continue
        url = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts
