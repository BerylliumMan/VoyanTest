# app/models/__init__.py
#
# Public surface of the app.models package.
#
# This package hosts two families of classes that share the same name
# (e.g. `Agent` exists as both a Pydantic schema and a SQLAlchemy ORM):
#
#   * Pydantic request/response schemas -> accessible as `app.models.Agent`
#     (preserves the historical `from app import models` contract used by
#     routers, see app/routers/agent_router.py and scheduler_router.py).
#
#   * SQLAlchemy ORM models -> split into domain sub-modules so that
#     `app.db_models` can stay a thin re-export hub while every model
#     class is still registered with `Base.metadata` the moment
#     `app.models` is imported (important for alembic autogenerate).
#
# Import the sub-modules here for their side effect: each one defines
# SQLAlchemy declarative classes that register themselves with Base.

# --- Pydantic schemas (re-exported for backwards compat) ---
from .schemas import (  # noqa: F401
    # auth
    LoginRequest, LoginResponse, ChangePasswordRequest,
    UserCreate, UserUpdate, UserResponse, ResetPasswordRequest,
    # project / environment / module
    ProjectBase, ProjectCreate, ProjectUpdate, Project,
    EnvironmentBase, EnvironmentCreate, EnvironmentUpdate, Environment,
    ModuleBase, ModuleCreate, ModuleUpdate, Module,
    # testcase
    TestCaseBase, TestCaseCreate, TestCaseUpdate, TestCase, TestCasePage,
    TestStepBase, TestStepCreate, TestStepCreatePayload, TestStepUpdate, TestStep,
    # batch
    RunBatchCreate, RunBatch, RunBatchUpdate, TestRun,
    # schedule
    ScheduleBase, ScheduleCreate, ScheduleUpdate, Schedule,
    # agent
    AgentCreate, AgentUpdate, Agent, AgentLog, AgentLogPage,
    # audit
    AuditLogResponse, AuditLogPage,
)

# --- SQLAlchemy ORM sub-modules (imported for side effects: register on Base.metadata) ---
from . import auth  # noqa: F401
from . import project  # noqa: F401
from . import testcase  # noqa: F401
from . import batch  # noqa: F401
from . import config  # noqa: F401
from . import agent  # noqa: F401
from . import gen  # noqa: F401
