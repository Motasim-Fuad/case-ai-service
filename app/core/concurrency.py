"""ThreadPool that propagates contextvars (so structured-log context follows into threads)."""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)
