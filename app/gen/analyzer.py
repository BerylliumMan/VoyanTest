"""AI generation orchestrator (back-compat shim).

Historically all generation logic lived in this single module. It now lives
in focused sub-modules:

- :mod:`app.gen.prompts`            - prompt templates and ``get_default_prompts``
- :mod:`app.gen.response_parser`   - parsing model output into dataclasses
- :mod:`app.gen.feature_extractor` - Phase 1 / Phase 2 model calls
- :mod:`app.gen.orchestrator`      - text/image/PDF two-phase pipelines
- :mod:`app.gen.multi_file`        - multi-file content extraction

This file re-exports the public API to keep existing imports working:
``from app.gen.analyzer import get_default_prompts, two_phase_analyze,
extract_multi_file_content`` and friends.
"""

from app.gen.constants import MAX_RETRIES, RETRY_DELAY
from app.gen.csv_generator import CSV_HEADER
from app.gen.feature_extractor import (
    extract_functional_points,
    generate_test_cases_for_fps,
)
from app.gen.models import AnalysisSession, FunctionalPoint, TestCase
from app.gen.multi_file import extract_multi_file_content
from app.gen.orchestrator import (
    MODEL_MAX_TOKENS,
    two_phase_analyze,
)
from app.gen.prompts import (
    FP_BATCH_SIZE,
    FP_EXTRACT_PROMPT,
    TC_GENERATE_PROMPT,
    get_default_prompts,
)
from app.gen.response_parser import (
    _clean_text,
    _parse_fps_from_text,
    _parse_response,
    _parse_tcs_from_text,
    _to_html,
)

__all__ = [
    # Constants
    "MODEL_MAX_TOKENS",
    "FP_BATCH_SIZE",
    "FP_EXTRACT_PROMPT",
    "TC_GENERATE_PROMPT",
    "CSV_HEADER",
    "MAX_RETRIES",
    "RETRY_DELAY",
    # Models
    "AnalysisSession",
    "FunctionalPoint",
    "TestCase",
    # Prompt helpers
    "get_default_prompts",
    # Parsing helpers
    "_clean_text",
    "_to_html",
    "_parse_response",
    "_parse_fps_from_text",
    "_parse_tcs_from_text",
    # Phase 1 / Phase 2 model calls
    "extract_functional_points",
    "generate_test_cases_for_fps",
    # Orchestrators
    "two_phase_analyze",
    # Multi-file extraction
    "extract_multi_file_content",
]
