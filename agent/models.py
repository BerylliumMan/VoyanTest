"""Agent communication protocol models (WebSocket-based)."""
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


class AgentStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"


# ---- Agent info (registration / listing) ----

class AgentInfo(BaseModel):
    id: str
    name: str
    hostname: str
    ip_address: str
    status: AgentStatus = AgentStatus.ONLINE
    last_seen: Optional[datetime] = None
    capabilities: List[str] = []
    current_task: Optional[str] = None


class AgentRegistration(BaseModel):
    name: str
    hostname: str
    ip_address: str
    capabilities: List[str] = []


# ---- WebSocket message types ----

class WSMessageType(str, Enum):
    # Server → Agent
    RUN_START = "run_start"
    STEP_EXECUTE = "step_execute"
    STEP_BROWSER_USE = "step_browser_use"  # hybrid: one NL step via browser-use on shared CDP
    RUN_COMPILED_SCRIPT = "run_compiled_script"  # whole-case solidified Playwright .py
    GET_SNAPSHOT = "get_snapshot"
    GET_SCREENSHOT = "get_screenshot"
    RUN_END = "run_end"
    SHUTDOWN = "shutdown"
    CANCEL_RUN = "cancel_run"  # best-effort: abort in-flight agent run
    RECORDING_START = "recording_start"
    RECORDING_STOP = "recording_stop"
    # Agent → Server
    REGISTERED = "registered"
    SNAPSHOT_RESULT = "snapshot_result"
    SCREENSHOT_RESULT = "screenshot_result"
    STEP_RESULT = "step_result"
    COMPILED_SCRIPT_RESULT = "compiled_script_result"
    RUN_COMPLETE = "run_complete"
    RUN_LOG = "run_log"  # Agent → Server: fire-and-forget progress log (no ACK)
    RECORDING_READY = "recording_ready"
    RECORDING_EVENTS = "recording_events"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class WSMessage(BaseModel):
    """Base WebSocket message envelope."""
    type: WSMessageType
    agent_id: str
    run_id: Optional[str] = None
    payload: Dict[str, Any] = {}


# ---- Step execution payloads ----

class StepExecutePayload(BaseModel):
    """Payload for STEP_EXECUTE: server tells agent to run one step."""
    step_order: int
    description: str
    tool_call: Dict[str, Any]  # PlaywrightMCPToolCall as dict


class StepResultPayload(BaseModel):
    """Payload for STEP_RESULT: agent reports step outcome."""
    step_order: int
    success: bool
    thinking: str = ""
    action: str = ""
    next_goal: str = ""
    error: Optional[str] = None
    duration_ms: float = 0
    screenshot_base64: Optional[str] = None


class SnapshotPayload(BaseModel):
    """Payload for SNAPSHOT_RESULT: agent returns page DOM snapshot."""
    text: str


class RunStartPayload(BaseModel):
    """Payload for RUN_START: server tells agent to begin a test run."""
    case_id: int
    case_name: str
    steps: List[Dict[str, Any]]  # [{step_order, description}]


class RecordingStartPayload(BaseModel):
    """Payload for RECORDING_START: server tells agent to start CDP recording."""
    url: str = ""
    headless: bool = False


class RecordingReadyPayload(BaseModel):
    """Payload for RECORDING_READY: agent reports CDP WebSocket URL."""
    cdp_url: str
    browser_type: str = "chromium"


class RunCompletePayload(BaseModel):
    """Payload for RUN_COMPLETE."""
    status: str  # "passed" or "failed"
    steps: List[Dict[str, Any]] = []
