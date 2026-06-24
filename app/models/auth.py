# app/models/auth.py
# 认证与用户管理 ORM 模型
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON

from app.database import Base
from app.tz import now as tz_now


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="tester")
    status = Column(String(50), nullable=False, default="active")
    locked_until = Column(DateTime(timezone=True), nullable=True)
    must_change_password = Column(Boolean, default=True)
    login_attempts = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    project_ids = Column(JSON, nullable=True, default=None)


class Session(Base):
    __tablename__ = "sessions"
    id = Column(String(64), primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_activity = Column(DateTime(timezone=True), default=tz_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(100), nullable=False)
    details = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
