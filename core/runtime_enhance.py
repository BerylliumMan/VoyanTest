# core/runtime_enhance.py
"""Thin runtime helpers for compiled_script runs (dialog / tracing / settle-retry).

Keep this module dependency-light so Agent frozen builds can ship it alone.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_DIALOG_FLAG = "_vt_native_dialog_handler"
_RETRYABLE_RE = re.compile(
    r"detached|Target closed|execution context|Timeout",
    re.I,
)


def install_native_dialog_handler(page, *, policy: str = "accept") -> None:
    """Register ``page.on('dialog')`` once. ``accept`` or ``dismiss``."""
    if getattr(page, _DIALOG_FLAG, False):
        return
    pol = (policy or "accept").strip().lower()
    if pol not in ("accept", "dismiss"):
        pol = "accept"

    async def _on_dialog(dialog) -> None:
        try:
            dtype = getattr(dialog, "type", "") or ""
            logger.info("native dialog type=%s policy=%s", dtype, pol)
            if pol == "dismiss":
                await dialog.dismiss()
            else:
                await dialog.accept()
        except Exception as exc:
            logger.warning("native dialog handler failed: %s", exc)

    page.on("dialog", _on_dialog)
    setattr(page, _DIALOG_FLAG, True)


class tracing_on_failure:
    """Start Playwright tracing; keep zip only when the block raises.

    If ``tracing.start`` fails (common on some CDP contexts), degrade silently
    and never block script execution.
    """

    def __init__(
        self,
        context,
        *,
        out_dir: str | Path,
        case_id: int = 0,
        enabled: bool = True,
    ) -> None:
        self.context = context
        self.out_dir = Path(out_dir)
        self.case_id = int(case_id or 0)
        self.enabled = bool(enabled)
        self._started = False
        self.trace_path: Optional[str] = None

    async def __aenter__(self) -> "tracing_on_failure":
        if not self.enabled:
            return self
        try:
            await self.context.tracing.start(
                screenshots=True, snapshots=True, sources=False,
            )
            self._started = True
        except Exception as exc:
            logger.warning("tracing unavailable (degraded): %s", exc)
            self._started = False
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._started:
            return None
        try:
            if exc_type is not None:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = self.out_dir / f"case_{self.case_id}_{ts}.zip"
                await self.context.tracing.stop(path=str(path))
                self.trace_path = str(path)
                logger.info("compiled_script trace saved: %s", self.trace_path)
            else:
                await self.context.tracing.stop()
        except Exception as stop_exc:
            logger.warning("tracing.stop failed: %s", stop_exc)
        return None


def _is_retryable(exc: BaseException) -> bool:
    if exc.__class__.__name__ == "TimeoutError":
        return True
    try:
        from playwright.async_api import TimeoutError as PwTimeout
        if isinstance(exc, PwTimeout):
            return True
    except Exception:
        pass
    if isinstance(exc, TimeoutError):
        return True
    return bool(_RETRYABLE_RE.search(str(exc) or ""))


async def run_with_settle_retry(
    run_once: Callable[[], Awaitable[None]],
    *,
    page,
    retries: int = 1,
    settle_ms: int = 800,
) -> None:
    """Run ``run_once``; on timeout/detached, settle and retry up to ``retries``."""
    attempts = max(0, int(retries)) + 1
    last: BaseException | None = None
    for i in range(attempts):
        try:
            await run_once()
            return
        except Exception as exc:
            last = exc
            if i + 1 >= attempts or not _is_retryable(exc):
                raise
            logger.info(
                "compiled_script settle-retry %s/%s after: %s",
                i + 1,
                attempts - 1,
                str(exc)[:180],
            )
            try:
                await page.wait_for_timeout(max(0, int(settle_ms)))
            except Exception:
                pass
    if last:
        raise last
