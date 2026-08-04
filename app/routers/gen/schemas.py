"""Pydantic models shared by the gen (AI test case generation) sub-routers.

These are defined in a dedicated module so that the upload/preview/import/
history sub-routers can all reuse the same request/response shapes without
causing circular imports.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class GenStatusResponse(BaseModel):
    session_id: str
    status: str  # pending / analyzing / completed / failed
    filename: str = ""
    error_message: str = ""
    message: str = ""  # live progress text for UI stepper
    progress: int = 0  # 0-100
    functional_points_count: int = 0
    test_cases_count: int = 0


class GenPreviewItem(BaseModel):
    test_case_id: str
    module: str
    title: str
    preconditions: str
    test_steps: str
    expected_result: str
    priority: str
    selected: bool = True
    validation_errors: str = ""
    structured_steps: list = []


class GenPreviewResponse(BaseModel):
    session_id: str
    filename: str = ""
    filenames: list[str] = []
    status: str = ""
    error_message: str = ""
    progress: int = 0
    progress_message: str = ""
    functional_points_count: int = 0
    test_cases_count: int = 0
    case_kind: str = "ui"  # ui | functional
    project_id: Optional[int] = None
    project_name: str = ""
    functional_points: list[dict]
    test_cases: list[GenPreviewItem]


class GenImportRequest(BaseModel):
    session_id: str
    project_id: int
    selected_ids: list[str] | None = None  # None = import all
    parent_module_id: int | None = None
    case_kind: str | None = None  # optional override: ui | functional


class GenImportResponse(BaseModel):
    imported_count: int
    skipped_count: int = 0
    test_case_ids: list[int]
    case_kind: str = "ui"
    project_id: int | None = None
    project_name: str = ""


class GenHistoryItem(BaseModel):
    id: str
    filename: str
    filenames: list[str]
    project_id: Optional[int] = None
    project_name: str = ""
    project_description: str
    status: str
    error_message: str
    progress: int = 0
    progress_message: str = ""
    functional_points_count: int
    test_cases_count: int
    imported_count: int
    case_kind: str = "ui"  # ui | functional
    created_at: datetime
    completed_at: Optional[datetime]
    can_retry: bool = False


class GenHistoryListResponse(BaseModel):
    items: list[GenHistoryItem]
    total: int


class GenTestCaseUpdate(BaseModel):
    module: Optional[str] = None
    title: Optional[str] = None
    preconditions: Optional[str] = None
    test_steps: Optional[str] = None
    expected_result: Optional[str] = None
    priority: Optional[str] = None
    validation_errors: Optional[str] = None
