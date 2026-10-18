#!/usr/bin/env python3
"""One think-loop tick.

The chat is a normal transcript: the human's messages are user turns, and the
final assistant message with no tool calls is what they see. A scheduled wake
with nothing new adds a [runtime] line so the mind can work on its own.
"""

import json
import os
import contextvars
import requests
import builtins

from clock import after_seconds_iso, format_duration, format_local, prompt_clock_line, utcnow, utcnow_iso
from context_budget import estimate_messages, fit_messages
from personality import personality_prompt_block
from tools import execute_tool, get_tools
from tools.context import ToolContext
from tools.mind_tools import resolve_next_wake
from transcript import build_llm_messages, choose_transcript, runtime_wake_line

_print = builtins.print
_log_name: contextvars.ContextVar[str] = contextvars.ContextVar("mind_log_name", default="")

_GLOBAL_TURN_LIMIT = 64
_FALLBACK_WAKE_SECONDS = 300
_TRANSCRIPT_LIMIT = 48


def set_log_name(name: str):
    return _log_name.set(name or "")


def reset_log_name(token) -> None:
    _log_name.reset(token)


def log(*args, sep=" ", end="\n"):
    ts = format_local(utcnow())
    who = _log_name.get()
    prefix = f"[{ts}]" + (f" {who}" if who else "")
    if not args:
        _print(prefix, end=end)
        return
    msg = sep.join(str(x) for x in args)
    leading = ""
    while msg.startswith("\n"):
        leading += "\n"
        msg = msg[1:]
    _print(f"{leading}{prefix} {msg}", end=end)


def message_text(content) -> str:
    """Visible text from an assistant message. Empty means stay quiet."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return "\n".join(parts).strip()
    return str(content).strip()


def _state_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_notes(
    store,
    *,
    human_name: str,
    session_seconds: float,
    total_uptime: float,
    earlier: bool,
    unseen_omitted: int,
) -> str:
    username = store.username
    parts = [
        f"Your name: {username}",
        f"Human: {human_name}",
        prompt_clock_line(),
        (
            f"Uptime: {format_duration(session_seconds)} this run, "
            f"{format_duration(total_uptime)} total."
        ),
    ]
    workspace = getattr(store, "tool_calls_dir", None)
    if workspace:
        parts.append(
            f"Private workspace (run_terminal starts here): {os.path.abspath(workspace)}"
        )
    personality = personality_prompt_block(store.get_state("personality"))
    if personality:
        parts.append(personality)
    focus = _state_text(store.get_state("focus"))
    goals = _state_text(store.get_state("goals"))
    next_steps = _state_text(store.get_state("next_steps"))
    if focus:
        parts.append(f"Focus: {focus}")
    if goals:
        parts.append(f"Goals:\n{goals}")
    if next_steps:
        parts.append(f"Next steps:\n{next_steps}")
    wake_reason = _state_text(store.get_state("wake_reason"))
    if wake_reason:
        parts.append(f"Why this loop started: {wake_reason}")
    last_loop_at = store.get_state("last_loop_at") or ""
    last_loop_status = _state_text(store.get_state("last_loop_status"))
    if last_loop_status or last_loop_at:
        when = format_local(last_loop_at) if last_loop_at else ""
        prev = last_loop_status or "n/a"
        parts.append("Previous loop: " + (f"{prev} at {when}" if when else prev))
    obs = store.recent_observations(10)
    if obs:
        parts.append("Recent private observations:")
        for o in obs:
            when = format_local(o.get("ts") or "")
            label = f"[{when}] " if when else ""
            parts.append(f"  • {label}{o.get('note', '')}")
    memory = store.get_memory_index()
    if memory:
        parts.append("Durable memory:\n" + memory)
    if earlier:
        parts.append("Earlier messages exist beyond this prompt. read_chat pages them.")
    if unseen_omitted > 0:
        parts.append(
            f"{unseen_omitted} older new message(s) did not fit in this prompt. read_chat can page them."
        )
    return "\n".join(parts)


def _apply_side_effects(store, name: str, args: dict):
    if name == "set_focus":
        text = (args.get("text") or "").strip()
        store.set_state("focus", text or None)
        log(f"🧠 focus: {text or '(cleared)'}")
    elif name == "log_observation":
        note = (args.get("note") or "").strip()
        if note:
            store.add_observation(note)
            log(f"🧠 observation: {note[:90]}")
    elif name == "plan_next_wake":
        resolved = resolve_next_wake(args.get("delay_seconds"), args.get("at"))
        if not resolved:
            return
        reason = (args.get("reason") or "")[:120]
        store.set_state("next_wake_after", resolved["next_wake_after"])
        store.set_state("next_wake_reason", reason)
        secs = resolved["delay_seconds"]
        at = resolved["at"]
        extra = f" ({reason})" if reason else ""
        if at:
            extra = f" at {at}" + extra
        log(f"🧠 next wake in ~{secs}s" + extra)
    elif name == "write_next_steps":
        steps = (args.get("steps") or "").strip()
        store.set_state("next_steps", steps)
        log(f"🧠 next_steps saved ({len(steps)} chars)")
    elif name == "update_goals":
        goals = (args.get("goals") or "").strip()
        store.set_state("goals", goals)
        log("🧠 goals updated")
    elif name == "update_personality":
        text = (args.get("text") or "").strip()
        store.set_state("personality", text or None)
        log(f"🧠 personality: {text[:90] or '(cleared)'}")
    elif name == "remember":
        title = (args.get("title") or "").strip()
        log(f"🧠 remembered: {title or '(empty)'}")
    elif name == "forget":
        title = (args.get("title") or "").strip()
        log(f"🧠 forgot: {title or '(empty)'}")


def _ensure_wake_plan(store, planned: bool, llm_ok: bool) -> None:
    if planned:
        return
    secs = _FALLBACK_WAKE_SECONDS
    store.set_state("next_wake_after", after_seconds_iso(secs))
    why = "LLM call failed" if not llm_ok else "model did not call plan_next_wake"
    store.set_state("next_wake_reason", f"fallback {secs}s ({why})")
    log(f"🧠 no plan_next_wake — defaulting to {secs}s ({why})")


def _configured_context(store) -> int:
    raw = store.get_config("max_context")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _attach_pending_images(messages: list, ctx: ToolContext) -> None:
    images = ctx.take_pending_images()
    if not images:
        return
    content = [{
        "type": "text",
        "text": "Image(s) from look_at_image are attached below. You can see them.",
    }]
    for img in images:
        label = (
            f"Image from look_at_image: {img.get('source')} "
            f"({img.get('mime')}, {img.get('nbytes')} bytes)"
        )
        focus = (img.get("focus") or "").strip()
        if focus:
            label += f"\nWhat to look at: {focus}"
        content.append({"type": "text", "text": label})
        content.append({"type": "image_url", "image_url": {"url": img["data_url"]}})
    messages.append({"role": "user", "content": content})
    log(f"  🖼  attached {len(images)} image(s) for vision")


def _tool_args(fn: dict) -> dict:
    raw = fn.get("arguments", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        args = json.loads(raw or "{}")
    except Exception:
        return {}
    return args if isinstance(args, dict) else {}


def run_think_loop(store, system_prompt: str, *, human_name: str, session_seconds: float, total_uptime: float) -> str:
    token = set_log_name(store.username)
    try:
        return _run_think_loop(
            store, system_prompt,
            human_name=human_name,
            session_seconds=session_seconds,
            total_uptime=total_uptime,
        )
    finally:
        reset_log_name(token)


def _run_think_loop(store, system_prompt: str, *, human_name: str, session_seconds: float, total_uptime: float) -> str:
    llm_base = (store.get_config("llm_base_url") or "").rstrip("/")
    llm_model = store.get_config("llm_model") or ""
    if not llm_base or not llm_model:
        return "error: llm_base_url / llm_model not configured"

    human_name = (human_name or "You").strip() or "You"
    last_seen = int(store.get_state("last_seen_message_id") or 0)
    # Captured before the call so a message that arrives mid-tick stays unseen.
    start_latest = store.latest_message_id()

    unseen = store.unseen_human(last_seen, limit=80)
    recent = store.recent_messages(80)
    transcript, trimmed = choose_transcript(recent, unseen, _TRANSCRIPT_LIMIT)
    earlier = trimmed
    if transcript and store.oldest_message_id() and int(transcript[0]["id"]) > store.oldest_message_id():
        earlier = True
    unseen_ids = {int(m["id"]) for m in unseen}
    shown_unseen = sum(1 for m in transcript if int(m["id"]) in unseen_ids)
    omitted = max(0, store.count_human_after(last_seen) - shown_unseen)

    wake_line = None
    if not any(int(m["id"]) in unseen_ids for m in transcript):
        reason = _state_text(store.get_state("wake_reason")) or "Scheduled wake"
        wake_line = runtime_wake_line(reason)

    notes = build_notes(
        store,
        human_name=human_name,
        session_seconds=session_seconds,
        total_uptime=total_uptime,
        earlier=earlier,
        unseen_omitted=omitted,
    )
    messages = build_llm_messages(
        system_prompt=system_prompt,
        notes=notes,
        transcript=transcript,
        unseen_ids=unseen_ids,
        human_name=human_name,
        wake_line=wake_line,
    )

    brave_key = store.get_config("brave_api_key") or ""
    ctx = ToolContext(
        tool_calls_dir=store.tool_calls_dir,
        brave_api_key=brave_key or None,
        store=store,
    )
    ctx.human_name = human_name
    tools = get_tools(web_search_enabled=bool(brave_key))
    max_context = _configured_context(store)

    turn = 0
    llm_ok = False
    planned_wake = False
    hit_step_limit = False
    used: list[str] = []
    reply = ""

    while turn < _GLOBAL_TURN_LIMIT:
        turn += 1
        if max_context:
            messages, budget_notes = fit_messages(messages, max_context, tools=tools)
            for note in budget_notes:
                log(f"🧠 context: {note}")
        payload = {
            "model": llm_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if max_context:
            used_tokens = estimate_messages(messages, tools)
            payload["max_tokens"] = max(1, max_context - used_tokens)
        try:
            r = requests.post(
                f"{llm_base}/chat/completions",
                json=payload,
                timeout=300,
            )
            if r.status_code != 200:
                log(f"LLM HTTP {r.status_code}: {r.text[:250]}")
                break
            msg = r.json().get("choices", [{}])[0].get("message", {})
            llm_ok = True
            messages.append(msg)
            if msg.get("tool_calls"):
                aside = message_text(msg.get("content"))
                if aside:
                    log(f"  (aside, not delivered): {aside[:120]}")
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    name = fn.get("name", "tool")
                    args = _tool_args(fn)
                    used.append(name)
                    if name == "run_terminal":
                        log(f"  🔧 run_terminal: {(args.get('command') or '')[:80]}")
                    elif name == "web_search":
                        log(f"  🔧 web_search: {(args.get('query') or '')[:80]}")
                    elif name == "look_at_image":
                        log(f"  🔧 look_at_image: {(args.get('source') or '')[:120]}")
                    elif name == "attach_file":
                        log(f"  🔧 attach_file: {(args.get('source') or '')[:120]}")
                    elif name == "search_chat":
                        log(f"  🔧 search_chat: {(args.get('query') or '')[:80]}")
                    else:
                        log(f"  🔧 {name}")
                    out = execute_tool(ctx, tc)
                    _apply_side_effects(store, name, args)
                    if name == "plan_next_wake":
                        try:
                            body = json.loads(out)
                        except Exception:
                            body = {}
                        if body.get("status") == "next wake scheduled":
                            planned_wake = True
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": out,
                    })
                _attach_pending_images(messages, ctx)
                continue
            reply = message_text(msg.get("content"))
            break
        except Exception as e:
            log("LLM exception:", e)
            break
    else:
        hit_step_limit = True
        log(f"🧠 step limit ({_GLOBAL_TURN_LIMIT}) reached")

    posted = False
    if llm_ok and (reply or ctx.outgoing):
        try:
            sent = store.add_message("mind", reply, ctx.outgoing)
            posted = True
            store.bump("replies")
            n_img = len(sent.get("attachments") or [])
            log(
                f"💬 sent #{sent.get('id')}: {(sent.get('text') or '')[:100]}"
                + (f" [{n_img} file(s)]" if n_img else "")
            )
        except Exception as e:
            log("Could not deliver reply:", e)
    elif llm_ok:
        log("🧠 no reply this loop")

    if llm_ok:
        store.set_state("last_seen_message_id", max(last_seen, start_latest))
    else:
        log("🧠 last_seen not advanced (no successful LLM response)")

    _ensure_wake_plan(store, planned_wake, llm_ok)
    store.set_state("last_loop_at", utcnow_iso())
    store.bump("loops")

    tool_names = list(dict.fromkeys(used))
    if not llm_ok:
        detail = "The model call failed."
    else:
        detail = ("Used " + ", ".join(tool_names)) if tool_names else "No tools"
        detail += ". Sent a reply." if posted else ". No reply."
    store.add_activity("loop", detail)

    status = f"turns={turn} reply={'yes' if posted else 'no'}"
    if tool_names:
        status += " tools=" + ",".join(tool_names)
    if not llm_ok:
        status += " llm_failed"
    if hit_step_limit:
        status += " step_limit"
    if not planned_wake:
        status += " no_wake_plan"
    store.set_state("last_loop_status", status)
    log(f"🧠 {status}")
    return status
