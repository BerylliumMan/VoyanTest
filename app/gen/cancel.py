"""Shared cancellation signal for long-running gen analysis tasks."""

CANCEL_MESSAGE = "用户已停止分析"


class GenAnalysisCancelled(Exception):
    """Raised when the user stops an in-flight analysis session."""

    def __init__(self, message: str = CANCEL_MESSAGE) -> None:
        super().__init__(message)
        self.message = message
