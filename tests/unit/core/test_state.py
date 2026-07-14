"""Tests for core/runner/_state.py — error classification."""
import pytest
from core.runner._state import _is_healable_error


class TestIsHealableError:
    def test_empty_string_returns_false(self):
        assert _is_healable_error("") is False

    def test_none_returns_false(self):
        assert _is_healable_error(None) is False

    def test_healable_element_not_found(self):
        assert _is_healable_error("element not found: #login-btn") is True

    def test_healable_selector_pattern(self):
        assert _is_healable_error(".selector .btn") is True

    def test_healable_timeout_exceeded(self):
        assert _is_healable_error("timeout exceeded 30000ms") is True

    def test_healable_could_not_find(self):
        assert _is_healable_error("could not find element") is True

    def test_healable_unable_to_find(self):
        assert _is_healable_error("unable to find locator") is True

    def test_healable_locator_error(self):
        assert _is_healable_error("locator error: button") is True

    def test_healable_waiting_for(self):
        assert _is_healable_error("waiting for selector") is True

    def test_non_healable_js_error(self):
        assert _is_healable_error("TypeError: Cannot read property") is False

    def test_non_healable_navigation_error(self):
        assert _is_healable_error("net::ERR_CONNECTION_REFUSED") is False

    def test_non_healable_assertion_error(self):
        assert _is_healable_error("AssertionError: expected true") is False

    def test_non_healable_empty_error(self):
        assert _is_healable_error("   ") is False

    def test_case_insensitive_healable(self):
        assert _is_healable_error("ELEMENT NOT FOUND") is True

    def test_partial_word_does_not_match(self):
        assert _is_healable_error("selector_value") is True
