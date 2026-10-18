# AGENTS.md

Guidance for working on marmot-mind-2.

## What this is

One process. Each **mind** has its own private chat with the human. There is no shared room, no @mentions, and no tags.

The model is given a normal transcript. The assistant message it writes when it is finished with tools is the chat reply. That is deliberate: a separate post tool and room controls were fighting models trained on ordinary chat.

Minds still run on their own. `plan_next_wake` schedules the next tick. A human message wakes that mind sooner. A scheduled wake with nothing new is a single `[runtime]` user line, not stored in the chat.

## Key locations

| Path | What |
|------|------|
| `code/app.py` | Flask app: UI, settings, per-mind chat API |
| `code/minds.py` | Loop threads, wake on a human message, uptime |
| `code/agent.py` | One think-loop tick |
| `code/transcript.py` | Timestamped transcript and the runtime wake line |
| `code/storage.py` | Per-mind SQLite, chat, uploads, memory pages |
| `code/attachments.py` | Image scaling and file saves |
| `code/clock.py` | UTC storage, host-local display |
| `code/context_budget.py` | Shrink a prompt to `max_context` |
| `code/tools/` | attach_file, look_at_image, read_chat, search_chat, terminal, web search, notes, memory, plan_next_wake |
| `code/prompts/system_prompt.txt` | Mechanics only. No job, no room |
| `code/templates/index.html` | Sidebar of minds, chat, settings |
| `start.sh` | Create the venv if needed and run the app |

## Running

```bash
./start.sh
./start.sh --create alice --start-loop \
  --llm-url http://HOST:8000/v1 --llm-model MODEL
```

Port: `MARMOT_PORT` or `--port` (default **5050**). Data dir: `data/` (override with `--data-dir`).

## Architecture

- **No login.** This is a local app. The human’s display name lives in `data/settings.json`.
- **One chat per mind**, stored in `data/minds/{name}/mind.db`. Deleting a mind removes the chat, memory, and uploads.
- **Names** are 2–32 characters, letters, numbers, `_`, `-`. Uniqueness is case-insensitive.
- **Timestamps** are stored as UTC. The UI and the prompt show the host’s local timezone (from `TZ`, `/etc/localtime`, or `/etc/timezone`). Every human message in the prompt is stamped, and so is every earlier reply. `GET /api/settings` includes `time_zone` so the page uses that same zone.
- **Images** (png/jpeg/gif/webp/bmp) are scaled to a 1024px long edge and saved as JPEG. Other files are stored as-is, up to 32MB. A message may have 8 attachments and at most 4 images. The prompt includes each file’s path.
- **New images** on an unseen human message are attached as vision content. Older ones stay as paths; `look_at_image` opens them.
- **The reply** is the final assistant message with no tool calls. Empty text sends nothing, unless `attach_file` queued a file. Tool-call turns are not delivered.
- **`last_seen_message_id`** advances only after a successful model response, and only up to the latest id from the start of that tick. A message that arrives mid-tick is picked up on the next tick.
- **Missing `plan_next_wake`** waits 5 minutes. A failed tick with an unread human message retries in about 30 seconds. Delays are clamped to about 20 seconds through 24 hours.
- **Uptime** accumulates while the loop thread is running, including sleep between wakes. It is flushed at the end of each tick and on stop. The header shows the current run. Settings shows the total.
- **Memory** is markdown pages under `data/minds/{name}/memory/topics/`. `remember` / `read_memory` / `forget`. `MEMORY.md` is generated.
- **`run_terminal`** is real bash in that mind’s `tool-calls/` directory, with no sandbox. A huge stdout or stderr is spilled to a file there. This is deliberate.
- **`max_context`** is optional. When set, older tool rounds are shortened so a reply still fits. The reply can use the room left in the window. Think-loop requests do not send `temperature`.
- Minds with `loop_enabled` and an LLM configured resume on startup.

## Tests

```bash
cd code && python3 -m unittest discover -s tests
```

No extra test dependencies. The app requirements are flask, requests, and pillow.
