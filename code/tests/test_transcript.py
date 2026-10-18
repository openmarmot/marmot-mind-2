import json
import os
import unittest
from datetime import datetime

from clock import display_zone, format_duration, format_local, prompt_clock_line, zone_name
from tools import get_tools
from tools.mind_tools import resolve_next_wake
from transcript import build_llm_messages, choose_transcript, render_for_model, runtime_wake_line


def _msg(mid, author, text, created_at="2026-10-18T18:00:00+00:00", attachments=None):
    return {
        "id": mid,
        "author": author,
        "text": text,
        "created_at": created_at,
        "attachments": attachments or [],
    }


class TranscriptTests(unittest.TestCase):
    def test_choose_keeps_unseen_ahead_of_old_chatter(self):
        recent = [_msg(i, "mind", f"m{i}") for i in range(1, 60)]
        unseen = [_msg(2, "human", "early")]
        chosen, trimmed = choose_transcript(recent, unseen, limit=10)
        self.assertTrue(trimmed)
        ids = [m["id"] for m in chosen]
        self.assertIn(2, ids)
        self.assertEqual(ids, sorted(ids))
        self.assertLessEqual(len(chosen), 10)

    def test_choose_caps_a_flood_of_unseen(self):
        unseen = [_msg(i, "human", f"h{i}") for i in range(1, 30)]
        chosen, trimmed = choose_transcript([], unseen, limit=5)
        self.assertTrue(trimmed)
        self.assertEqual([m["id"] for m in chosen], [25, 26, 27, 28, 29])

    def test_short_chat_is_not_trimmed(self):
        recent = [_msg(1, "human", "hi"), _msg(2, "mind", "hello")]
        chosen, trimmed = choose_transcript(recent, [recent[0]], limit=10)
        self.assertFalse(trimmed)
        self.assertEqual([m["id"] for m in chosen], [1, 2])

    def test_human_message_is_timestamped_and_named(self):
        created = "2026-10-18T18:00:00+00:00"
        text = render_for_model(_msg(1, "human", "look at this", created_at=created), "Andrew")
        local = datetime.fromisoformat(created).astimezone(display_zone())
        self.assertIn("Andrew:", text)
        self.assertIn(local.strftime("%Y-%m-%d %H:%M:%S"), text)
        self.assertIn(local.tzname() or "local", text)
        self.assertIn("look at this", text)

    def test_runtime_line_only_when_nobody_wrote(self):
        human = _msg(4, "human", "status?")
        messages = build_llm_messages(
            system_prompt="You are a mind.",
            notes="Your name: ada",
            transcript=[human],
            unseen_ids={4},
            human_name="Andrew",
            wake_line=None,
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Your name: ada", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertNotIn("[runtime", messages[1]["content"])
        self.assertIn("status?", messages[1]["content"])

        quiet = build_llm_messages(
            system_prompt="You are a mind.",
            notes="",
            transcript=[_msg(3, "mind", "on it")],
            unseen_ids=set(),
            human_name="Andrew",
            wake_line=runtime_wake_line("scheduled: check the build"),
        )
        self.assertTrue(quiet[-1]["content"].startswith("[runtime "))
        self.assertIn("not a person", quiet[-1]["content"])
        self.assertEqual(quiet[-2]["role"], "assistant")

    def test_missing_image_file_stays_text(self):
        msg = _msg(1, "human", "pic", attachments=[{
            "kind": "image",
            "name": "gone.jpg",
            "path": "/no/such/file.jpg",
        }])
        from transcript import message_to_llm
        out = message_to_llm(msg, vision=True, human_name="Andrew")
        self.assertIsInstance(out["content"], str)
        self.assertIn("gone.jpg", out["content"])

    def test_tools_are_not_a_chat_room(self):
        names = [tool["function"]["name"] for tool in get_tools(True)]
        self.assertIn("plan_next_wake", names)
        self.assertIn("attach_file", names)
        self.assertIn("read_chat", names)
        self.assertNotIn("post_message", names)
        self.assertNotIn("read_messages", names)
        self.assertNotIn("search_messages", names)
        wake = next(tool for tool in get_tools() if tool["function"]["name"] == "plan_next_wake")
        self.assertNotIn("America/Phoenix", json.dumps(wake))

    def test_wake_clamp_and_duration(self):
        soon = resolve_next_wake(1)
        self.assertGreaterEqual(soon["delay_seconds"], 20)
        long = resolve_next_wake(999999)
        self.assertLessEqual(long["delay_seconds"], 24 * 3600)
        self.assertEqual(format_duration(90), "1m")
        self.assertEqual(format_duration(3700), "1h 1m")
        self.assertEqual(format_duration(90000), "1d 1h")

    def test_prompt_does_not_describe_a_room(self):
        path = os.path.join(os.path.dirname(__file__), "..", "prompts", "system_prompt.txt")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("chat room", text.lower())
        self.assertNotIn("post_message", text)
        self.assertNotIn("@everyone", text)
        self.assertNotIn("America/Phoenix", text)
        self.assertIn("local clock", text)
        self.assertIn("plan_next_wake", text)
        self.assertIn("[runtime]", text)


class ClockTests(unittest.TestCase):
    def test_utc_renders_in_the_host_zone(self):
        raw = "2026-10-18T18:30:00+00:00"
        local = datetime.fromisoformat(raw).astimezone(display_zone())
        shown = format_local(raw)
        self.assertTrue(shown.startswith(local.strftime("%Y-%m-%d %H:%M:%S")))
        self.assertIn(local.tzname() or "local", shown)

    def test_prompt_names_the_host_zone(self):
        line = prompt_clock_line()
        self.assertIn("Current time:", line)
        name = zone_name()
        if name:
            self.assertIn(name, line)


if __name__ == "__main__":
    unittest.main()
