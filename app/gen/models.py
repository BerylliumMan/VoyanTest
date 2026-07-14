from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
import uuid


def new_session_id() -> str:
    return str(uuid.uuid4())


@dataclass
class AnalysisSession:
    session_id: str = field(default_factory=new_session_id)
    filename: str = ""
    file_type: str = ""
    file_size: int = 0
    upload_time: datetime = field(default_factory=datetime.now)
    status: str = "pending"
    error_message: str = ""
    project_description: str = ""
    filenames: list = field(default_factory=list)
    completed_at: Optional[datetime] = None
    functional_points: list["FunctionalPoint"] = field(default_factory=list)
    test_cases: list["TestCase"] = field(default_factory=list)


@dataclass
class FunctionalPoint:
    id: int = 0
    session_id: str = ""
    module: str = ""
    name: str = ""
    description: str = ""
    category: str = ""


@dataclass
class TestCase:
    test_case_id: str = ""
    session_id: str = ""
    module: str = ""
    title: str = ""
    preconditions: str = ""
    test_steps: str = ""
    expected_result: str = ""
    priority: str = "中"
