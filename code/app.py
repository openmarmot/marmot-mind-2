#!/usr/bin/env python3
"""Marmot Mind — one process, one private chat per mind.

The human uses the web UI. Each mind reads and replies in its own transcript
and wakes on a new message or on the schedule it set.
"""

import argparse
import json
import os
import sys

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.serving import WSGIRequestHandler

from attachments import MAX_IMAGE_BYTES, discard_uploads, store_uploads
from clock import zone_label, zone_name
from minds import MindManager, valid_username

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
DEFAULT_DATA_DIR = os.path.join(REPO_ROOT, "data")
PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "system_prompt.txt")
DEFAULT_PORT = int(os.environ.get("MARMOT_PORT", "5050"))
MAX_TEXT = 16000

_minds: MindManager | None = None
_settings_path: str | None = None


def _load_system_prompt() -> str:
    try:
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            return content
    except Exception as e:
        print("Warning: could not load system prompt:", e)
    return (
        "You are a mind with your own private chat. "
        "The message you write when you are done with tools is delivered to the human."
    )


def _fix_url(u: str) -> str:
    u = (u or "").strip()
    if u and not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u.rstrip("/")


def _read_settings() -> dict:
    try:
        with open(_settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(_settings_path), exist_ok=True)
    tmp = _settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, _settings_path)


def _public_max_context(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _parse_max_context(value):
    """Positive token count, or None to clear. Raises ValueError on garbage."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("max_context must be a positive number of tokens")
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("_", "")
        if not value:
            return None
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("max_context must be a whole number of tokens")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_context must be a positive number of tokens")
    if n <= 0:
        return None
    if n > 2_000_000:
        raise ValueError("max_context must be at most 2000000")
    return n


def _display_name(data: dict | None = None) -> str:
    data = _read_settings() if data is None else data
    name = (data.get("display_name") or "").strip()
    return name[:40] or "You"


def _public_settings(data: dict | None = None) -> dict:
    data = _read_settings() if data is None else data
    return {
        "display_name": _display_name(data),
        "llm_base_url": data.get("llm_base_url") or "",
        "llm_model": data.get("llm_model") or "",
        "max_context": _public_max_context(data.get("max_context")),
        "brave_api_key_set": bool(data.get("brave_api_key")),
        "time_zone": zone_name(),
        "time_zone_label": zone_label(),
    }


def _apply_llm_fields(setter, body: dict) -> None:
    """Persist non-blank LLM fields. A blank brave key clears it when the key is present."""
    if "llm_base_url" in body:
        url = _fix_url(body.get("llm_base_url") or "")
        if url:
            setter("llm_base_url", url)
    if "llm_model" in body:
        model = (body.get("llm_model") or "").strip()
        if model:
            setter("llm_model", model)
    if "brave_api_key" in body:
        key = (body.get("brave_api_key") or "").strip()
        setter("brave_api_key", key or None)
    if "max_context" in body:
        setter("max_context", _parse_max_context(body.get("max_context")))


def _mind_or_404(username: str):
    rt = _minds.get(username) if _minds else None
    if rt is None:
        return None, (jsonify({"error": "unknown mind"}), 404)
    return rt, None


def _sorted_minds() -> list:
    rows = []
    for rt in _minds.all():
        snap = rt.snapshot()
        preview = snap.get("preview") or {}
        stamp = preview.get("at") or snap.get("created_at") or ""
        rows.append((stamp, snap["username"].lower(), snap))
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    # Newest chat first, but an empty stamp sorts last once reversed... 
    # An empty string is smallest, so reverse puts empty first. Split them.
    dated = [item for item in rows if item[0]]
    undated = [item for item in rows if not item[0]]
    dated.sort(key=lambda item: item[0], reverse=True)
    undated.sort(key=lambda item: item[1])
    return [item[2] for item in dated + undated]


def create_app(data_dir: str | None = None) -> Flask:
    global _minds, _settings_path
    data_dir = data_dir or DEFAULT_DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    mind_root = os.path.join(data_dir, "minds")
    os.makedirs(mind_root, exist_ok=True)
    _settings_path = os.path.join(data_dir, "settings.json")
    _minds = MindManager(mind_root, _load_system_prompt(), _read_settings)
    _minds.load_existing()

    app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        running = [rt for rt in _minds.all() if rt.snapshot()["loop_running"]]
        return jsonify({
            "status": "ok",
            "service": "marmot-mind-2",
            "minds": len(_minds.usernames()),
            "minds_running": len(running),
        })

    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(_public_settings())

    @app.post("/api/settings")
    def api_settings_post():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object body required"}), 400
        data = _read_settings()

        def setter(key, value):
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value

        try:
            _apply_llm_fields(setter, body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if "display_name" in body:
            name = (body.get("display_name") or "").strip()[:40]
            data["display_name"] = name or "You"
        _write_settings(data)
        return jsonify({"ok": True, "settings": _public_settings(data)})

    @app.get("/api/minds")
    def api_minds():
        return jsonify({"minds": _sorted_minds()})

    @app.post("/api/minds")
    def api_minds_create():
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        err = valid_username(username)
        if err:
            return jsonify({"error": err if username else "name required"}), 400
        settings = _read_settings()
        try:
            rt = _minds.create(
                username,
                llm_base_url=settings.get("llm_base_url") or None,
                llm_model=settings.get("llm_model") or None,
                brave_api_key=settings.get("brave_api_key") or None,
                max_context=_public_max_context(settings.get("max_context")),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except FileExistsError as e:
            return jsonify({"error": str(e)}), 409
        started = False
        warning = None
        if body.get("start"):
            try:
                rt.start()
                started = True
            except Exception as e:
                warning = str(e)
        return jsonify({
            "ok": True,
            "started": started,
            "warning": warning,
            "mind": rt.snapshot(),
        }), 201

    @app.get("/api/minds/<username>")
    def api_mind_get(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        return jsonify(rt.snapshot())

    @app.post("/api/minds/<username>/config")
    def api_mind_config(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object body required"}), 400
        try:
            _apply_llm_fields(rt.store.set_config, body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "mind": rt.snapshot()})

    @app.post("/api/minds/<username>/start")
    def api_mind_start(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        try:
            rt.start()
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "mind": rt.snapshot()})

    @app.post("/api/minds/<username>/stop")
    def api_mind_stop(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        rt.stop()
        return jsonify({"ok": True, "mind": rt.snapshot()})

    @app.post("/api/minds/<username>/tick")
    def api_mind_tick(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        try:
            status = rt.tick_once()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"status": status, "mind": rt.snapshot()})

    @app.delete("/api/minds/<username>")
    def api_mind_delete(username):
        try:
            name = _minds.delete(username)
        except FileNotFoundError:
            return jsonify({"error": "unknown mind"}), 404
        return jsonify({"ok": True, "username": name})

    @app.get("/api/minds/<username>/messages")
    def api_messages(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        limit = request.args.get("limit", 50)
        try:
            limit_n = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit_n = 50
        after = request.args.get("after")
        before = request.args.get("before")
        store = rt.store
        if after not in (None, ""):
            try:
                messages = store.messages_after(int(after), limit_n)
            except (TypeError, ValueError):
                return jsonify({"error": "after must be an integer"}), 400
        elif before not in (None, ""):
            try:
                messages = store.messages_before(int(before), limit_n)
            except (TypeError, ValueError):
                return jsonify({"error": "before must be an integer"}), 400
        else:
            messages = store.recent_messages(limit_n)
        oldest = store.oldest_message_id()
        has_earlier = bool(messages) and int(messages[0]["id"]) > oldest
        return jsonify({
            "messages": messages,
            "latest_id": store.latest_message_id(),
            "count": store.message_count(),
            "has_earlier": has_earlier,
        })

    @app.post("/api/minds/<username>/messages")
    def api_messages_post(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        if request.is_json:
            body = request.get_json(silent=True) or {}
            text = body.get("text") or ""
            blobs = []
        else:
            text = request.form.get("text") or ""
            blobs = []
            for f in request.files.getlist("files"):
                if f is None or not (f.filename or "").strip():
                    continue
                data = f.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    return jsonify({"error": "file is too large"}), 400
                blobs.append((data, f.filename))
        text = str(text)
        if len(text) > MAX_TEXT:
            return jsonify({"error": f"message is longer than {MAX_TEXT} characters"}), 400
        if not text.strip() and not blobs:
            return jsonify({"error": "empty message"}), 400
        saved = []
        try:
            if blobs:
                saved = store_uploads(rt.store, blobs)
            msg = rt.store.add_message("human", text.strip(), saved)
        except ValueError as e:
            discard_uploads(rt.store, saved)
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            discard_uploads(rt.store, saved)
            return jsonify({"error": str(e)}), 500
        _minds.notify(rt.username)
        return jsonify({"ok": True, "message": msg}), 201

    @app.post("/api/minds/<username>/clear")
    def api_clear(username):
        rt, err = _mind_or_404(username)
        if err:
            return err
        cleared = rt.store.clear_messages()
        return jsonify({"ok": True, "cleared": cleared, "latest_id": 0, "count": 0})

    @app.get("/api/minds/<username>/files/<fid>")
    def api_file(username, fid):
        rt, err = _mind_or_404(username)
        if err:
            return err
        rec = rt.store.get_attachment(fid)
        if not rec:
            return jsonify({"error": "not found"}), 404
        path = os.path.realpath(rec.get("path") or "")
        root = os.path.realpath(rt.store.uploads_dir)
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            return jsonify({"error": "not found"}), 404
        return send_file(
            path,
            mimetype=rec.get("mime") or "application/octet-stream",
            download_name=rec.get("name") or "file",
            as_attachment=rec.get("kind") != "image",
            conditional=True,
        )

    return app


class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        path = self.path.split("?", 1)[0]
        if "/files/" in path:
            return
        if path in ("/api/minds", "/health") and str(code).startswith("2"):
            return
        if path.endswith("/messages") and str(code).startswith("2"):
            return
        super().log_request(code, size)


def main():
    parser = argparse.ArgumentParser(description="Marmot Mind — one chat per mind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--create", metavar="NAME", help="Create a mind, then serve")
    parser.add_argument("--llm-url", default=None, help="Default LLM base URL (also applied to --create)")
    parser.add_argument("--llm-model", default=None, help="Default LLM model (also applied to --create)")
    parser.add_argument("--max-context", type=int, default=None, help="Default context window in tokens")
    parser.add_argument("--start-loop", action="store_true", help="Start the --create mind's think loop")
    args = parser.parse_args()

    if args.start_loop and not args.create:
        print("--start-loop requires --create NAME")
        sys.exit(2)

    app = create_app(args.data_dir)

    if args.llm_url or args.llm_model or args.max_context:
        data = _read_settings()

        def setter(key, value):
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value

        fields = {}
        if args.llm_url:
            fields["llm_base_url"] = args.llm_url
        if args.llm_model:
            fields["llm_model"] = args.llm_model
        if args.max_context:
            fields["max_context"] = args.max_context
        _apply_llm_fields(setter, fields)
        _write_settings(data)

    if args.create:
        settings = _read_settings()
        try:
            rt = _minds.create(
                args.create,
                llm_base_url=settings.get("llm_base_url") or None,
                llm_model=settings.get("llm_model") or None,
                brave_api_key=settings.get("brave_api_key") or None,
                max_context=_public_max_context(settings.get("max_context")),
            )
        except (ValueError, FileExistsError) as e:
            print(e)
            sys.exit(1)
        if args.start_loop:
            try:
                rt.start()
            except Exception as e:
                print(f"Could not start loop: {e}")
                sys.exit(1)

    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    print()
    print("🐹 Marmot Mind")
    print(f"   Web UI : http://127.0.0.1:{args.port}/")
    print(f"   Data   : {args.data_dir}")
    print(f"   Minds  : {', '.join(_minds.usernames()) or '(none yet)'}")
    print()
    app.run(
        host="0.0.0.0",
        port=args.port,
        threaded=True,
        request_handler=QuietRequestHandler,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
