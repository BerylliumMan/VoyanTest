# app/models/recording.py - CDP recording session ORM model

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class RecordingSession(Base):
    __tablename__ = "recording_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    url = Column(String(2048), default="")
    page_title = Column(String(512), default="")
    status = Column(String(20), default="recording")  # recording / stopped
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    events_count = Column(Integer, default=0)
    converted = Column(Boolean, default=False)

    project = relationship("Project", backref="recording_sessions")
