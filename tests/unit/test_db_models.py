"""Tests for app/db_models.py — ORM model definitions."""
from app.db_models import (
    User,
    Session,
    TestCase,
    Agent,
    RunBatch,
    Project,
    Module,
    RunLog,
    TestStep,
    AuditLog,
    AIConfig,
    Environment,
)


class TestModelConstruction:
    def test_user_creation(self):
        u = User(username="admin", role="admin")
        assert u.username == "admin"
        assert u.role == "admin"

    def test_session_creation(self):
        s = Session(id="abc", user_id=1)
        assert s.id == "abc"
        assert s.user_id == 1

    def test_run_batch_creation(self):
        b = RunBatch(id=1, project_id=1, total_cases=5)
        assert b.project_id == 1
        assert b.total_cases == 5

    def test_testcase_defaults(self):
        tc = TestCase(name="test", project_id=1)
        assert tc.name == "test"
        assert tc.steps is not None

    def test_project_creation(self):
        p = Project(name="My Project", base_url="https://example.com")
        assert p.name == "My Project"
        assert p.base_url == "https://example.com"
