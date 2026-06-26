# app/models/notification.py - Notification ORM model

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(50), nullable=False, default="info")  # info / success / warning / error
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=True)
    read = Column(Boolean, default=False)
    batch_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
