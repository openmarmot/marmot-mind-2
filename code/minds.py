"""Several minds in one process. Each mind has its own chat and think loop.

A human message wakes that mind immediately. Otherwise it sleeps until the
time it set with plan_next_wake. There is no shared room.
"""

import os
import secrets
import shutil
import threading
import time

from agent import log, reset_log_name, run_think_loop, set_log_name
from clock import seconds_until, utcnow_iso
from storage import MindStore, list_usernames

_MAX_SLEEP_SECONDS = 24 * 3600
_DEFAULT_SLEEP_SECONDS = 300.0


def _seconds_until_wake(store: MindStore) -> float:
    """Seconds until next_wake_after. Overdue → 0. Missing → 5 min. Cap 24h."""
    nw = store.get_state("next_wake_after")
    if not nw:
        return _DEFAULT_SLEEP_SECONDS
    delta = seconds_until(nw)
    if delta is None:
        return _DEFAULT_SLEEP_SECONDS
    if delta <= 0:
        return 0.0
    return min(float(_MAX_SLEEP_SECONDS), delta)


def valid_username(username: str) -> str | None:
    username = (username or "").strip()
    if len(username) < 2 or len(username) > 32:
        return "username must be 2–32 characters"
    if not all(c.isalnum() or c in "_-" for c in username):
        return "username may only contain letters, numbers, _ and -"
    return None


class MindRuntime:
    def __init__(self, store: MindStore, system_prompt: str, settings_getter):
        self.store = store
        self.system_prompt = system_prompt
        self.settings_getter = settings_getter
        self.loop_stop = threading.Event()
        self.loop_wake = threading.Event()
        self.tick_lock = threading.Lock()
        self.loop_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.loop_thread: threading.Thread | None = None
        self.loop_running = False
        self.thinking = False
        self.suppress_until_id = 0
        self._session_mark = time.monotonic()
        self._uptime_mark = time.monotonic()

    @property
    def username(self) -> str:
        return self.store.username

    def llm_ready(self) -> bool:
        return bool(self.store.get_config("llm_base_url") and self.store.get_config("llm_model"))

    def on_human_message(self) -> None:
        """Wake so this mind can read a new human message.

        A message that arrives during a tick is picked up when that tick
        finishes and looks again. Safe to call from the request thread.
        """
        if not self.store.get_config("loop_enabled"):
            return
        latest = self.store.latest_message_id()
        if latest <= int(self.store.get_state("last_seen_message_id") or 0):
            return
        if latest <= self.suppress_until_id:
            return
        if self.tick_lock.locked():
            return
        with self.state_lock:
            if self.tick_lock.locked():
                return
            first = not self.loop_wake.is_set()
            self.store.set_state("wake_reason", "message from the human")
            self.loop_wake.set()
        if first:
            token = set_log_name(self.username)
            try:
                log("👀 message from the human — waking")
            finally:
                reset_log_name(token)

    def _unseen_human(self) -> bool:
        last_seen = int(self.store.get_state("last_seen_message_id") or 0)
        floor = max(last_seen, self.suppress_until_id)
        return self.store.count_human_after(floor) > 0

    def _human_name(self) -> str:
        try:
            data = self.settings_getter() or {}
        except Exception:
            data = {}
        name = (data.get("display_name") or "").strip()
        return name or "You"

    def _uptime_pending(self) -> float:
        if not self.loop_running:
            return 0.0
        return max(0.0, time.monotonic() - self._uptime_mark)

    def _flush_uptime(self) -> None:
        if not self.loop_running:
            return
        now = time.monotonic()
        delta = now - self._uptime_mark
        self._uptime_mark = now
        if delta > 0:
            self.store.add_uptime(delta)

    def run_tick(self, reason: str | None = None) -> tuple[str, bool]:
        """One think cycle. Returns (status, should_think_again)."""
        if reason:
            self.store.set_state("wake_reason", reason)
        self.thinking = True
        try:
            with self.tick_lock:
                status = run_think_loop(
                    self.store,
                    self.system_prompt,
                    human_name=self._human_name(),
                    session_seconds=max(0.0, time.monotonic() - self._session_mark) if self.loop_running else 0.0,
                    total_uptime=float(self.store.get_state("uptime_seconds") or 0) + self._uptime_pending(),
                )
        finally:
            self.thinking = False
            self._flush_uptime()
        with self.state_lock:
            self.loop_wake.clear()
        failed = "llm_failed" in (status or "")
        if not failed:
            self.suppress_until_id = 0
        unseen = self._unseen_human()
        if unseen and not failed:
            self.store.set_state("wake_reason", "message from the human")
            return status, True
        if unseen and failed:
            self.suppress_until_id = max(self.suppress_until_id, self.store.latest_message_id())
            return status, False
        self.suppress_until_id = 0
        return status, False

    def _loop_body(self):
        token = set_log_name(self.username)
        try:
            self._loop_body_inner()
        finally:
            reset_log_name(token)

    def _loop_body_inner(self):
        log("🧠 Think loop started")
        if self.loop_stop.wait(2):
            self.loop_running = False
            log("🧠 Think loop stopped")
            return
        last_seen = int(self.store.get_state("last_seen_message_id") or 0)
        if self.store.count_human_after(last_seen):
            self.store.set_state("wake_reason", "message from the human")
        elif not self.store.get_state("wake_reason"):
            self.store.set_state("wake_reason", "loop start")
        while not self.loop_stop.is_set():
            try:
                if not self.store.get_config("loop_enabled"):
                    self.loop_wake.wait(timeout=5)
                    self.loop_wake.clear()
                    continue
                try:
                    status, again = self.run_tick()
                    log(f"🧠 tick: {status}")
                except Exception as e:
                    log("Think loop error:", e)
                    self.store.set_state("last_loop_status", f"error: {e}")
                    self.store.set_state("last_loop_at", utcnow_iso())
                    self.store.add_activity("loop", f"Error: {e}")
                    again = False
                if again and not self.loop_stop.is_set():
                    continue
                last_seen_now = int(self.store.get_state("last_seen_message_id") or 0)
                if self.suppress_until_id > last_seen_now and not self.loop_stop.is_set():
                    log("🧠 unread message after a failed tick — retrying in 30s")
                    self.store.set_state("next_wake_reason", "retry after failed tick")
                    self.loop_wake.wait(timeout=30)
                    self.loop_wake.clear()
                    self.suppress_until_id = 0
                    if not self.loop_stop.is_set():
                        self.store.set_state("wake_reason", "retry unread message")
                    continue
                sleep_secs = _seconds_until_wake(self.store)
                log(f"🧠 sleeping ~{int(sleep_secs)}s (a new message wakes immediately)")
                woke = self.loop_wake.wait(timeout=sleep_secs)
                self.loop_wake.clear()
                if self.loop_stop.is_set():
                    break
                if woke:
                    reason = (self.store.get_state("wake_reason") or "").strip()
                    if reason:
                        log(f"🧠 woken early by {reason}")
                    else:
                        log("🧠 woken early")
                        self.store.set_state("wake_reason", "interrupted")
                else:
                    nxt = (self.store.get_state("next_wake_reason") or "").strip()
                    self.store.set_state(
                        "wake_reason",
                        f"scheduled: {nxt}" if nxt else "scheduled",
                    )
            except Exception as e:
                log("Loop outer error:", e)
                time.sleep(30)
        self.loop_running = False
        log("🧠 Think loop stopped")

    def start(self):
        with self.loop_lock:
            if not self.llm_ready():
                raise RuntimeError("Set an LLM endpoint and model first")
            self.store.set_config("loop_enabled", True)
            now = time.monotonic()
            self.loop_running = True
            if self.loop_thread and self.loop_thread.is_alive():
                self.store.set_state("wake_reason", "loop start")
                self.loop_wake.set()
                return
            self._session_mark = now
            self._uptime_mark = now
            self.store.set_state("running_since", utcnow_iso())
            self.loop_stop.clear()
            self.loop_wake.clear()
            self.suppress_until_id = 0
            self.store.add_activity("loop", "Loop started")
            self.loop_thread = threading.Thread(
                target=self._loop_body, daemon=True, name=f"mind-{self.username}"
            )
            self.loop_thread.start()

    def stop(self):
        with self.loop_lock:
            was_running = self.loop_running
            self._flush_uptime()
            self.store.set_config("loop_enabled", False)
            self.store.set_state("running_since", None)
            self.loop_stop.set()
            self.loop_wake.set()
            self.loop_running = False
            if was_running:
                self.store.add_activity("loop", "Loop stopped")

    def tick_once(self) -> str:
        if not self.llm_ready():
            raise RuntimeError("Set an LLM endpoint and model first")
        status, again = self.run_tick("manual tick")
        if again and self.loop_running:
            self.loop_wake.set()
        return status

    def snapshot(self) -> dict:
        loop_on = bool(
            self.loop_running
            and self.loop_thread
            and self.loop_thread.is_alive()
            and self.store.get_config("loop_enabled")
        )
        pending = self._uptime_pending() if loop_on else 0.0
        session = max(0.0, time.monotonic() - self._session_mark) if loop_on else 0.0
        stored = float(self.store.get_state("uptime_seconds") or 0)
        topics = self.store.list_topics()
        return {
            "username": self.username,
            "created_at": self.store.get_config("created_at"),
            "loop_running": loop_on,
            "thinking": bool(self.thinking),
            "llm_ready": self.llm_ready(),
            "config": {
                "llm_base_url": self.store.get_config("llm_base_url") or "",
                "llm_model": self.store.get_config("llm_model") or "",
                "max_context": self.store.get_config("max_context") or None,
                "loop_enabled": bool(self.store.get_config("loop_enabled")),
                "brave_api_key_set": bool(self.store.get_config("brave_api_key")),
            },
            "uptime_seconds": round(stored + pending, 3),
            "session_seconds": round(session, 3),
            "running_since": self.store.get_state("running_since") if loop_on else None,
            "loops": int(self.store.get_state("loops") or 0),
            "replies": int(self.store.get_state("replies") or 0),
            "personality": self.store.get_state("personality") or "",
            "focus": self.store.get_state("focus") or "",
            "goals": self.store.get_state("goals") or "",
            "next_steps": self.store.get_state("next_steps") or "",
            "next_wake_after": self.store.get_state("next_wake_after"),
            "wake_reason": self.store.get_state("wake_reason") or "",
            "next_wake_reason": self.store.get_state("next_wake_reason") or "",
            "last_loop_at": self.store.get_state("last_loop_at"),
            "last_loop_status": self.store.get_state("last_loop_status") or "",
            "last_seen_message_id": self.store.get_state("last_seen_message_id") or 0,
            "recent_observations": self.store.recent_observations(8),
            "memory_topics": [
                {"title": t["title"], "summary": t["summary"]}
                for t in topics
            ],
            "activity": self.store.recent_activity(12),
            "preview": self.store.latest_preview(),
            "latest_message_id": self.store.latest_message_id(),
            "message_count": self.store.message_count(),
        }


class MindManager:
    def __init__(self, mind_root: str, system_prompt: str, settings_getter):
        self.mind_root = mind_root
        self.system_prompt = system_prompt
        self.settings_getter = settings_getter
        self._minds: dict[str, MindRuntime] = {}
        self._lock = threading.Lock()

    def notify(self, username: str) -> None:
        rt = self.get(username)
        if rt is None:
            return
        try:
            rt.on_human_message()
        except Exception as e:
            log(f"wake error for {username}: {e}")

    def all(self) -> list[MindRuntime]:
        with self._lock:
            return list(self._minds.values())

    def usernames(self) -> list[str]:
        return [rt.username for rt in self.all()]

    def get(self, username: str) -> MindRuntime | None:
        key = (username or "").strip().lower()
        with self._lock:
            for name, rt in self._minds.items():
                if name.lower() == key:
                    return rt
        return None

    def load_existing(self) -> None:
        if os.path.isdir(self.mind_root):
            for name in os.listdir(self.mind_root):
                if ".deleting-" not in name:
                    continue
                shutil.rmtree(os.path.join(self.mind_root, name), ignore_errors=True)
        for name in list_usernames(self.mind_root):
            if self.get(name):
                continue
            store = MindStore(self.mind_root, name)
            rt = MindRuntime(store, self.system_prompt, self.settings_getter)
            with self._lock:
                self._minds[store.username] = rt
            if store.get_config("loop_enabled") and rt.llm_ready():
                try:
                    rt.start()
                    log(f"🧠 Auto-resumed {store.username}")
                except Exception as e:
                    log(f"Could not auto-start {store.username}: {e}")

    def create(
        self,
        username: str,
        *,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        brave_api_key: str | None = None,
        max_context: int | None = None,
    ) -> MindRuntime:
        username = (username or "").strip()
        err = valid_username(username)
        if err:
            raise ValueError(err)
        with self._lock:
            known = {n.lower() for n in list_usernames(self.mind_root)}
            if username.lower() in known or any(n.lower() == username.lower() for n in self._minds):
                raise FileExistsError("a mind with that name already exists")
            store = MindStore(self.mind_root, username)
            store.set_config("created_at", utcnow_iso())
            store.set_config("loop_enabled", False)
            store.set_state("last_seen_message_id", 0)
            if llm_base_url:
                store.set_config("llm_base_url", llm_base_url)
            if llm_model:
                store.set_config("llm_model", llm_model)
            if brave_api_key:
                store.set_config("brave_api_key", brave_api_key)
            if max_context:
                store.set_config("max_context", int(max_context))
            rt = MindRuntime(store, self.system_prompt, self.settings_getter)
            self._minds[store.username] = rt
            log(f"✨ Created mind {username}")
            return rt

    def delete(self, username: str) -> str:
        """Stop the mind and remove its files, including its chat.

        An in-flight tick is allowed to finish. The directory is renamed
        immediately so a restart will not bring the mind back.
        """
        rt = self.get(username)
        if rt is None:
            raise FileNotFoundError(username)
        name = rt.username
        rt.stop()
        with self._lock:
            self._minds.pop(name, None)
        src = rt.store.dir
        trash = src + ".deleting-" + secrets.token_hex(4)
        try:
            os.rename(src, trash)
        except OSError as e:
            log(f"Could not move {name} aside: {e}")
            trash = src
        thread = rt.loop_thread

        def _finish():
            if thread and thread.is_alive():
                thread.join(timeout=360)
            shutil.rmtree(trash, ignore_errors=True)
            log(f"🗑 Deleted mind {name}")

        threading.Thread(target=_finish, daemon=True, name=f"delete-{name}").start()
        return name
