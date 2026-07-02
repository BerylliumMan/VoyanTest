"""Pydantic models shared by the recording sub-routers.

These are defined in a dedicated module so that the start/stop/status/list/
convert sub-routers can all reuse the same request/response shapes without
causing circular imports.
"""
from pydantic import BaseModel


class StartRecordingRequest(BaseModel):
    url: str
    page_title: str = ""
    agent_name: str | None = None


class RecordingStatusResponse(BaseModel):
    session_id: str
    status: str  # recording / stopped / completed
    url: str
    page_title: str = ""
    elapsed_seconds: float = 0.0
    events_count: int = 0
    error_message: str = ""


class RecordedEventResponse(BaseModel):
    event_type: str
    timestamp: float
    selector: str | None = None
    value: str | None = None
    url: str = ""
    page_title: str = ""
    screenshot: str | None = None


class RecordingListResponse(BaseModel):
    sessions: list[RecordingStatusResponse]


class ConvertRequest(BaseModel):
    session_id: str


class ConvertStepItem(BaseModel):
    step_description: str
    expected_result: str


class ConvertResponse(BaseModel):
    session_id: str
    page_title: str = ""
    steps: list[ConvertStepItem]
    events_count: int = 0


class SaveAsCaseRequest(BaseModel):
    project_id: int
    module_id: int | None = None
    name: str
    steps: list[ConvertStepItem]


class SaveAsCaseResponse(BaseModel):
    case_id: int
    name: str
    steps_count: int
