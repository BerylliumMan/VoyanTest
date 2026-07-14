"""Tests for core/log_setup.py — structured logging."""
import logging
import json
import sys
import pytest
from core.log_setup import (
    JsonFormatter,
    set_request_id,
    get_request_id,
    setup_logging,
    request_id_var,
)


@pytest.fixture(autouse=True)
def _reset_request_id():
    request_id_var.set(None)
    yield


class TestRequestIdContext:
    def test_set_and_get(self):
        set_request_id("test-123")
        assert get_request_id() == "test-123"

    def test_default_is_none(self):
        rid = get_request_id()
        assert rid is None


class TestJsonFormatter:
    def test_format_basic_record(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            "test_logger", logging.INFO, "test.py", 1,
            "hello world", (), None,
        )
        output = json.loads(fmt.format(record))
        assert output["level"] == "INFO"
        assert output["logger"] == "test_logger"
        assert output["msg"] == "hello world"
        assert "ts" in output

    def test_format_includes_request_id(self):
        set_request_id("req-456")
        fmt = JsonFormatter()
        record = logging.LogRecord(
            "test", logging.WARNING, "t.py", 1, "msg", (), None,
        )
        output = json.loads(fmt.format(record))
        assert output["request_id"] == "req-456"

    def test_format_includes_exception(self):
        fmt = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "test", logging.ERROR, "t.py", 1,
                "error occurred", (), exc_info=sys.exc_info(),
        )
        output = json.loads(fmt.format(record))
        assert output["exception"]["type"] == "ValueError"
        assert output["exception"]["message"] == "boom"


class TestSetupLogging:
    def test_json_format(self):
        setup_logging("DEBUG", "json")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) > 0

    def test_text_format(self):
        setup_logging("INFO", "text")
        root = logging.getLogger()
        assert root.level == logging.INFO
