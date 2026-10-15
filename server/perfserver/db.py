"""Metadata + statistics live in a relational DB (SQLite for dev, MySQL/TiDB/Postgres in prod).
Raw samples live in blob storage, see blob.py."""
import os
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./perfscope.db")
engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                       connect_args={"check_same_thread": False}
                       if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    # external_id: reserved for mapping to the company tool platform's project later
    external_id = Column(String(128), index=True)
    name = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TestSession(Base):
    __tablename__ = "sessions"
    id = Column(String(64), primary_key=True)  # same id as the client, makes re-upload idempotent
    project_id = Column(Integer, ForeignKey("projects.id"), index=True)
    name = Column(String(255))
    platform = Column(String(32), index=True)
    device_model = Column(String(128), index=True)
    os_version = Column(String(64))
    app_package = Column(String(255), index=True)
    app_version = Column(String(64), index=True)
    tags = Column(JSON, default=list)
    notes = Column(Text, default="")
    device = Column(JSON)
    config = Column(JSON)
    # display info for each metric, sent by the client (label / unit / better / order)
    metric_meta = Column(JSON, default=dict)
    started_at = Column(BigInteger, index=True)  # epoch ms
    ended_at = Column(BigInteger)
    duration_s = Column(Float)
    status = Column(String(32))
    point_count = Column(Integer)
    blob_key = Column(String(512))
    created_by = Column(String(128), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship(Project)
    stats = relationship("MetricStat", cascade="all, delete-orphan", lazy="selectin")
    events = relationship("Event", cascade="all, delete-orphan", lazy="selectin",
                          order_by="Event.ts")


class MetricStat(Base):
    __tablename__ = "metric_stats"
    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey("sessions.id"), index=True)
    metric = Column(String(128), index=True)
    count = Column(Integer)
    avg = Column(Float)
    min = Column(Float)
    max = Column(Float)
    p50 = Column(Float)
    p90 = Column(Float)
    p99 = Column(Float)
    first = Column(Float)
    last = Column(Float)
    slope_per_hour = Column(Float)  # linear trend, mainly for memory leak detection


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey("sessions.id"), index=True)
    ts = Column(BigInteger)
    type = Column(String(32))
    label = Column(String(512))


def init_db():
    Base.metadata.create_all(engine)
