"""Read this mind's own chat. There is no shared room."""

import json

from transcript import render_for_model

_READ_CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": "read_chat",
        "description": (
            "Read earlier messages in this chat. "
            "before_id pages older than that id. Each message is timestamped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "before_id": {
                    "type": "integer",
                    "description": "Return messages older than this id. Omit for the latest page.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many messages (default 40, max 80).",
                },
            },
        },
    },
}

_SEARCH_CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_chat",
        "description": "Find messages in this chat whose text contains a phrase. Case-insensitive.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Phrase to find in message text."},
                "limit": {
                    "type": "integer",
                    "description": "How many hits (default 20, max 40).",
                },
            },
            "required": ["query"],
        },
    },
}


def _limit(value, default: int, cap: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(cap, n))


def _format(messages: list, human_name: str) -> str:
    if not messages:
        return "(no messages)"
    return "\n\n".join(render_for_model(m, human_name) for m in messages)


def _human_name(ctx) -> str:
    store = getattr(ctx, "store", None)
    if store is None:
        return "Human"
    name = store.get_state("_human_name")
    # The display name is passed in on the context when the loop starts.
    name = getattr(ctx, "human_name", None) or name
    return (name or "Human").strip() or "Human"


def execute_read_chat(ctx, before_id=None, limit=None) -> str:
    store = getattr(ctx, "store", None) if ctx else None
    if store is None:
        return json.dumps({"status": "error", "message": "chat unavailable"})
    try:
        before = int(before_id) if before_id not in (None, "") else None
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "before_id must be an integer"})
    messages = store.messages_before(before, _limit(limit, 40, 80))
    return _format(messages, _human_name(ctx))


def execute_search_chat(ctx, query: str, limit=None) -> str:
    store = getattr(ctx, "store", None) if ctx else None
    if store is None:
        return json.dumps({"status": "error", "message": "chat unavailable"})
    q = (query or "").strip()
    if not q:
        return json.dumps({"status": "error", "message": "empty query"})
    messages = store.search_messages(q, _limit(limit, 20, 40))
    if not messages:
        return f"No messages contain: {q}"
    return _format(messages, _human_name(ctx))
