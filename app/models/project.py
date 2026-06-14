# app/models/project.py
# 项目、环境、模块 ORM 模型
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship

from app.database import Base
from app.tz import now as tz_now


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    base_url = Column(String, nullable=True)
    browser = Column(String, default="chromium")
    headless = Column(Boolean, default=True)
    created_at = Column(DateTime, default=tz_now)

    test_cases = relationship("TestCase", backref="project", cascade="save-update, merge")
    environments = relationship("Environment", backref="project", cascade="all, delete-orphan")


class Environment(Base):
    __tablename__ = "environments"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    browser = Column(String, default="chromium")
    headless = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    # 预置 cookie 列表（执行前注入到浏览器上下文，避免重复登录）
    # 结构: [{"name": "session", "value": "xxx", "domain": "example.com", "path": "/"}]
    cookies = Column(JSON, default=list)
    created_at = Column(DateTime, default=tz_now)


class Module(Base):
    __tablename__ = "modules"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    parent_id = Column(Integer, ForeignKey("modules.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=tz_now)

    test_cases = relationship("TestCase", backref="module", passive_deletes=True)
    children = relationship("Module", backref="parent", remote_side=[id])
