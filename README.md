# marmot-mind-2

Fresh rebuild of the original marmot-mind concept that drops the chat room and focuses on find tuning the minds.

## Quick start

```bash
./start.sh
```

Open **http://127.0.0.1:5050/**

- The left pane is your minds. Create one with **+**.
- The main pane is that mind’s chat. The composer takes text, images, and other files (attach, paste, or drop).
- **Settings** (the gear in the chat header) is that mind’s model endpoint, context window, search key, uptime, and what it has been doing.
- The gear at the bottom sets your name and the defaults copied onto a new mind.

A mind does not think until it has an endpoint and a model and the loop is started. **Start** is in the header when the loop is off, and in settings.

Optional:

```bash
MARMOT_PORT=8080 ./start.sh
./start.sh --create alice \
  --llm-url http://127.0.0.1:8000/v1 \
  --llm-model your-model \
  --start-loop
```

Requires an OpenAI-compatible LLM (`/v1/chat/completions`) before a mind can think.

## How a mind thinks

One loop per mind:

1. The prompt is a normal transcript. Your messages are user turns, timestamped in this computer’s local timezone. The mind’s earlier replies are assistant turns.
2. The final assistant message, the one with no tool call, is delivered into the chat. Text written next to a tool call stays private. An empty final message sends nothing.
3. `attach_file` adds an image or file to that reply. `look_at_image` shows the mind a picture.
4. `plan_next_wake` sets the next think (`delay_seconds`, or `at` in local time, from about 20 seconds up to a day). If it does not call that, the loop waits 5 minutes. A new message from you wakes it sooner.
5. On a scheduled wake with nothing new from you, the runtime adds one `[runtime]` line. That line is the clock, not a person. The mind can use tools and stay quiet.
6. Private notes (focus, goals, next steps, observations, memory pages) come back on the next loop. `run_terminal` is bash in that mind’s own `tool-calls/` directory. It is not sandboxed.

State for each mind lives in `data/minds/{name}/` (the chat is in that mind’s `mind.db`). Defaults live in `data/settings.json`. Both are created on first run. Nothing is imported from marmot-mind.

Uptime is the time the loop has actually been running, including the wait between wakes. Settings shows this run and the total across starts.

## Tests

```bash
cd code && python3 -m unittest discover -s tests
```
