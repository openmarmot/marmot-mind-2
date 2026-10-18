import io
import os
import tempfile
import unittest

from PIL import Image

import app as app_module
from app import create_app
from clock import zone_label, zone_name
from attachments import store_upload
from storage import MindStore


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 4), (20, 80, 200)).save(buf, format="PNG")
    return buf.getvalue()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MindStore(self.tmp.name, "ada")

    def tearDown(self):
        self.tmp.cleanup()

    def test_message_is_timestamped_and_keeps_a_file(self):
        rec = store_upload(self.store, b"hello file", "notes.txt")
        self.assertEqual(rec["kind"], "file")
        msg = self.store.add_message("human", "see this", [rec])
        self.assertEqual(msg["author"], "human")
        self.assertIn("T", msg["created_at"])
        self.assertTrue(msg["attachments"][0]["name"].endswith("notes.txt"))
        self.assertTrue(os.path.isfile(msg["attachments"][0]["path"]))
        again = self.store.messages_after(0)
        self.assertEqual(again[0]["id"], msg["id"])
        self.assertEqual(self.store.count_human_after(0), 1)
        self.assertEqual(self.store.count_human_after(msg["id"]), 0)

    def test_image_upload_becomes_jpeg(self):
        rec = store_upload(self.store, _png_bytes(), "shot.png")
        self.assertEqual(rec["kind"], "image")
        self.assertEqual(rec["mime"], "image/jpeg")
        self.assertTrue(rec["name"].endswith(".jpg"))

    def test_clear_and_uptime(self):
        self.store.add_message("human", "one")
        self.assertEqual(self.store.clear_messages(), 1)
        self.assertEqual(self.store.message_count(), 0)
        self.assertEqual(self.store.get_state("last_seen_message_id"), 0)
        self.store.add_uptime(12.5)
        self.store.add_uptime(1)
        self.assertEqual(self.store.get_state("uptime_seconds"), 13.5)

    def test_search_and_memory(self):
        self.store.add_message("human", "the gate code is 4412")
        self.store.add_message("mind", "got it")
        hits = self.store.search_messages("4412")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["author"], "human")
        written = self.store.write_topic("Gate", "Code is 4412.")
        self.assertEqual(written["slug"], "gate")
        self.assertIn("4412", self.store.read_topic("Gate")["body"])
        self.assertIn("Gate", self.store.get_memory_index())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.flask = create_app(self.tmp.name)
        self.flask.config["TESTING"] = True
        self.client = self.flask.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_and_page(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["service"], "marmot-mind-2")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Create a mind", html)
        self.assertIn("Mind settings", html)
        self.assertIn("Message", html)
        self.assertIn("this computer's clock", html)
        self.assertIn("[hidden] { display: none !important; }", html)
        self.assertIn("These settings belong to", html)
        self.assertIn("Close settings", html)
        self.assertNotIn("America/Phoenix", html)

    def test_settings_hide_the_key(self):
        saved = self.client.post("/api/settings", json={
            "display_name": "Andrew",
            "llm_base_url": "http://127.0.0.1:8000/v1",
            "llm_model": "demo",
            "max_context": "32000",
            "brave_api_key": "secret-key",
        })
        self.assertEqual(saved.status_code, 200)
        body = saved.get_json()["settings"]
        self.assertEqual(body["display_name"], "Andrew")
        self.assertEqual(body["llm_model"], "demo")
        self.assertEqual(body["max_context"], 32000)
        self.assertEqual(body["time_zone"], zone_name())
        self.assertEqual(body["time_zone_label"], zone_label())
        self.assertTrue(body["brave_api_key_set"])
        self.assertNotIn("secret-key", saved.get_data(as_text=True))
        cleared = self.client.post("/api/settings", json={"brave_api_key": ""})
        self.assertFalse(cleared.get_json()["settings"]["brave_api_key_set"])

    def test_chat_roundtrip_with_image_and_file(self):
        created = self.client.post("/api/minds", json={"username": "Ada", "start": True})
        self.assertEqual(created.status_code, 201)
        body = created.get_json()
        self.assertEqual(body["mind"]["username"], "Ada")
        self.assertFalse(body["started"])
        self.assertTrue(body["warning"])

        self.client.post("/api/settings", json={
            "llm_base_url": "http://127.0.0.1:8000/v1",
            "llm_model": "demo",
        })
        made = self.client.post("/api/minds", json={"username": "Bea", "start": False})
        self.assertEqual(made.status_code, 201)
        # Defaults apply to minds created after they are saved.
        self.assertEqual(made.get_json()["mind"]["config"]["llm_model"], "demo")

        data = {
            "text": "two attachments",
        }
        posted = self.client.post(
            "/api/minds/Bea/messages",
            data={
                "text": "two attachments",
                "files": [
                    (io.BytesIO(_png_bytes()), "shot.png"),
                    (io.BytesIO(b"alpha,beta\n"), "table.csv"),
                ],
            },
        )
        self.assertEqual(posted.status_code, 201, posted.get_data(as_text=True))
        msg = posted.get_json()["message"]
        self.assertEqual(msg["author"], "human")
        self.assertTrue(msg["created_at"])
        kinds = sorted(att["kind"] for att in msg["attachments"])
        self.assertEqual(kinds, ["file", "image"])

        listed = self.client.get("/api/minds/Bea/messages")
        self.assertEqual(listed.get_json()["count"], 1)
        image = next(att for att in msg["attachments"] if att["kind"] == "image")
        fetched = self.client.get(image["url"])
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.mimetype, "image/jpeg")
        fetched.close()

        minds = self.client.get("/api/minds").get_json()["minds"]
        bea = next(m for m in minds if m["username"] == "Bea")
        self.assertIn("two attachments", bea["preview"]["text"])

        cleared = self.client.post("/api/minds/Bea/clear")
        self.assertEqual(cleared.get_json()["cleared"], 1)
        self.assertEqual(self.client.get("/api/minds/Bea/messages").get_json()["count"], 0)

        gone = self.client.delete("/api/minds/Bea")
        self.assertEqual(gone.status_code, 200)
        self.assertIsNone(app_module._minds.get("Bea"))

    def test_bad_name_and_empty_message(self):
        self.assertEqual(self.client.post("/api/minds", json={"username": "x"}).status_code, 400)
        self.client.post("/api/minds", json={"username": "Ada"})
        self.assertEqual(
            self.client.post("/api/minds/Ada/messages", json={"text": "   "}).status_code,
            400,
        )
        ok = self.client.post("/api/minds/Ada/messages", json={"text": "hello"})
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.get_json()["message"]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
