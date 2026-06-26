# app/db_models.py
#
# Re-export hub for SQLAlchemy ORM models.
#
# Models are physically defined under `app.models.*` (one domain per file).
# This module preserves the historical `from app.db_models import <Model>`
# surface so existing call sites (routers, crud, core, agent, alembic env)
# keep working unchanged.
#
# Adding a new model:
#   1. Define the SQLAlchemy class in the appropriate domain sub-module
#      under `app/models/` (each file imports `Base` from `app.database`).
#   2. Add a re-export line below.

# Re-export the SQLAlchemy declarative Base so call sites that historically
# did `from app.db_models import Base` (alembic env, app.main) keep working.
from app.database import Base  # noqa: F401

# Auth / users / audit
from app.models.auth import (  # noqa: F401
    User, Session, AuditLog, ApiKey,
)

# Project / environment / module
from app.models.project import (  # noqa: F401
    Project, Environment, Module,
)

# Test cases and steps
from app.models.testcase import (  # noqa: F401
    TestCase, TestStep,
)

# Run batch / runs / logs
from app.models.batch import (  # noqa: F401
    RunBatch, TestRun, RunLog,
)

# AI config and prompt templates
from app.models.config import (  # noqa: F401
    AIConfig, PromptTemplate,
)

# Distributed agent and agent log
from app.models.agent import (  # noqa: F401
    Agent, AgentLog,
)

# AI generation sessions, functional points, generated test cases,
# scheduled tasks and scheduled task runs
from app.models.gen import (  # noqa: F401
    GenSession, GenFunctionalPoint, GenTestCase,
    ScheduledTask, ScheduledTaskRun,
)

# CDP recording sessions
from app.models.recording import RecordingSession  # noqa: F401
