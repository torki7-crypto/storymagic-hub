from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, default="")
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    stories = relationship("SavedStory", back_populates="owner")


class SavedStory(Base):
    __tablename__ = "saved_stories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    title = Column(String, nullable=False)
    hero_name = Column(String, default="")
    age = Column(Integer, default=7)
    theme = Column(String, default="")
    style = Column(String, default="")
    language_code = Column(String, default="en")
    is_paid = Column(Boolean, default=False)
    stripe_session_id = Column(String, nullable=True, default="")  # holds the Square order id
    plan_tier = Column(String, nullable=True, default="digital")   # tier chosen at checkout (not yet paid)
    paid_tier = Column(String, nullable=True)                      # tier actually paid for (set by webhook)
    lulu_job_id = Column(String, nullable=True)                    # prevents double print orders
    story_data = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="stories")
