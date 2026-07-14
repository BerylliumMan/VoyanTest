"""Tests for app/utils.py."""
import pytest
from unittest.mock import MagicMock
from app.utils import client_ip


class TestClientIP:
    def test_uses_x_forwarded_for(self):
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "203.0.113.1, 10.0.0.1"}
        request.client = None
        assert client_ip(request) == "203.0.113.1"

    def test_uses_first_ip_when_multiple(self):
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
        request.client = None
        assert client_ip(request) == "1.2.3.4"

    def test_falls_back_to_client_host(self):
        request = MagicMock()
        request.headers = {}
        request.client.host = "192.168.1.1"
        assert client_ip(request) == "192.168.1.1"

    def test_returns_empty_when_nothing_available(self):
        request = MagicMock()
        request.headers = {}
        request.client = None
        assert client_ip(request) == ""
