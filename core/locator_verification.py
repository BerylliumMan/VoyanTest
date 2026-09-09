"""Deterministic evidence checks for locator actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LocatorActionEvidence:
    """Observed state before and after one browser action."""

    action: str
    before_snapshot: str
    after_snapshot: str
    observed_value: str | None = None
    expected_value: str | None = None
    url_changed: bool = False


@dataclass(frozen=True, slots=True)
class LocatorVerification:
    """Deterministic result for an action evidence check."""

    verified: bool
    failure_kind: str | None = None


def verify_locator_action(evidence: LocatorActionEvidence) -> LocatorVerification:
    """Verify state changes without an additional LLM request."""
    action = evidence.action.lower()
    if action in {"fill", "type"}:
        if evidence.expected_value is None:
            return LocatorVerification(False, "verification_missing_expected_value")
        if evidence.observed_value != evidence.expected_value:
            return LocatorVerification(False, "input_value_mismatch")
        return LocatorVerification(True)
    if action == "select":
        if evidence.expected_value is None:
            return LocatorVerification(False, "verification_missing_expected_value")
        if evidence.observed_value != evidence.expected_value:
            return LocatorVerification(False, "selected_value_mismatch")
        return LocatorVerification(True)
    if action in {"click", "press", "hover"}:
        if evidence.url_changed or evidence.before_snapshot != evidence.after_snapshot:
            return LocatorVerification(True)
        return LocatorVerification(False, "no_observable_change")
    return LocatorVerification(True)
