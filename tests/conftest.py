# tests/conftest.py
import os
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# 使用内存 SQLite（xdist 下 StaticPool 确保各 worker 共享同一 DB）
TEST_DATABASE_URL = "sqlite:///:memory:"

# 在导入 app 之前覆盖环境变量
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.database import Base, get_db, SessionLocal
from app.main import app
from app import db_models
from app.auth import hash_password


# ==================== 测试数据库引擎 ====================

@pytest.fixture(scope="session")
def engine():
    """创建测试用数据库引擎，session 级别共享。（:memory: 模式使用 StaticPool 跨 session 共享）"""
    eng = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 20}, poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)



@pytest.fixture(scope="function")
def db(engine):
    """每个测试函数独立的数据库会话。

    使用 PRAGMA foreign_keys=OFF 简化清理，最后再 ON。
    """
    import app.database as db_mod
    TestSessionLocal = sessionmaker(bind=engine)
    original_session_local = db_mod.SessionLocal
    db_mod.SessionLocal = TestSessionLocal

    session = TestSessionLocal()

    yield session

    try:
        from sqlalchemy import text
        session.execute(text("PRAGMA foreign_keys = OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        session.execute(text("PRAGMA foreign_keys = ON"))
    except Exception:
        session.rollback()
    finally:
        session.close()
        db_mod.SessionLocal = original_session_local


@pytest.fixture(scope="function")
def client(db):
    """FastAPI TestClient，注入测试数据库会话。"""
    from app.rate_limiter import limiter

    def override_get_db():
        try:
            yield db
        finally:
            pass

    # 禁用速率限制（装饰器闭包里绑定的是原始 limiter 的 self.enabled）
    limiter.enabled = False

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    limiter.enabled = True


# ==================== 认证辅助 ====================

@pytest.fixture
def admin_user(db):
    """确保测试数据库中存在管理员用户。"""
    user = db.query(db_models.User).filter(db_models.User.username == "admin").first()
    if not user:
        user = db_models.User(
            username="admin",
            password_hash=hash_password("Admin@2024"),
            role="admin",
            status="active",
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # 确保 must_change_password 为 False（main.py 启动时可能创建了 True 的默认管理员）
        if user.must_change_password:
            user.must_change_password = False
            db.commit()
    return user


def _ensure_admin_user_body(db):
    """设置干净的 admin 用户（可单独调用，也可被 fixture 使用）。"""
    from app.auth import hash_password as _hp
    from app import db_models as _m
    user = db.query(_m.User).filter(_m.User.username == "admin").first()
    if user is None:
        user = _m.User(
            username="admin",
            password_hash=_hp("Admin@2024"),
            role="admin", status="active", must_change_password=False,
        )
        db.add(user)
    else:
        user.password_hash = _hp("Admin@2024")
        user.role = "admin"
        user.status = "active"
        user.must_change_password = False
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def ensure_admin_user(db):
    """设置干净的 admin 用户 — must_change_password=False, 密码 = Admin@2024。"""
    return _ensure_admin_user_body(db)


@pytest.fixture
def tester_user(db):
    """创建普通测试员用户。"""
    user = db.query(db_models.User).filter(db_models.User.username == "tester1").first()
    if not user:
        user = db_models.User(
            username="tester1",
            password_hash=hash_password("Tester@123"),
            role="tester",
            status="active",
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


@pytest.fixture
def admin_cookies(client, db, admin_user):
    """登录管理员并返回认证 cookies。"""
    resp = client.post("/api/auth/login", json={
        "username": "admin",
        "password": "Admin@2024",
    })
    assert resp.status_code == 200, f"管理员登录失败: {resp.json()}"
    return resp.cookies


@pytest.fixture
def tester_cookies(client, db, tester_user):
    """登录测试员并返回认证 cookies。"""
    resp = client.post("/api/auth/login", json={
        "username": "tester1",
        "password": "Tester@123",
    })
    assert resp.status_code == 200, f"测试员登录失败: {resp.json()}"
    return resp.cookies


# ==================== 数据辅助 ====================

@pytest.fixture
def sample_project(client, admin_cookies):
    """创建示例项目并返回其数据。"""
    resp = client.post("/api/projects/", json={
        "name": "测试项目",
        "description": "用于测试的项目",
        "base_url": "https://example.com",
        "browser": "chromium",
        "headless": True,
    }, cookies=admin_cookies)
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture
def sample_module(client, admin_cookies, sample_project):
    """创建示例模块并返回其数据。"""
    pid = sample_project["id"]
    resp = client.post(f"/api/projects/{pid}/modules", json={
        "project_id": pid,
        "name": "测试模块",
        "description": "用于测试的模块",
    }, cookies=admin_cookies)
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture
def sample_testcase(client, admin_cookies, sample_project):
    """创建示例测试用例并返回其数据。"""
    pid = sample_project["id"]
    resp = client.post("/api/testcases/", json={
        "project_id": pid,
        "name": "测试用例1",
        "description": "用于测试的用例",
        "steps": [
            {"step_order": 1, "description": "打开首页"},
            {"step_order": 2, "description": "点击登录按钮"},
        ],
    }, cookies=admin_cookies)
    assert resp.status_code == 200
    return resp.json()
