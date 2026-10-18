"""Per-think-loop tool state. Minds run concurrently, so nothing here is process-global."""


class ToolContext:
    def __init__(self, tool_calls_dir: str, brave_api_key: str | None = None, store=None):
        self.tool_calls_dir = tool_calls_dir
        self.brave_api_key = (brave_api_key or "").strip() or None
        self.store = store
        self.pending_images: list[dict] = []
        # Files and images queued onto the reply that ends this loop.
        self.outgoing: list[dict] = []

    def take_pending_images(self) -> list[dict]:
        out = self.pending_images
        self.pending_images = []
        return out
