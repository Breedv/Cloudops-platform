from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse
import logging
import time
import os

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey,
    Integer, String, create_engine, select
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session,
    mapped_column, sessionmaker
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cloudops")

# ---------- Database ----------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./cloudops.db",
)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utc_now():
    return datetime.now(timezone.utc)


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(2048))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_status: Mapped[str] = mapped_column(
        String(20), default="UNKNOWN"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_time_ms: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id")
    )
    message: Mapped[str] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Request schema ----------

class MonitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)


def validate_target_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(
            status_code=422,
            detail="Enter a valid HTTP or HTTPS URL.",
        )


# ---------- Monitoring logic ----------

def check_monitor(monitor_id: int):
    db = SessionLocal()
    try:
        monitor = db.get(Monitor, monitor_id)
        if not monitor or not monitor.is_active:
            return

        started = time.perf_counter()
        status = "DOWN"
        message = "The service did not return a successful HTTP response."
        response_time = None

        try:
            with httpx.Client(
                timeout=5.0,
                follow_redirects=True,
            ) as client:
                response = client.get(monitor.url)

            response_time = round(
                (time.perf_counter() - started) * 1000, 2
            )

            if response.status_code < 400:
                status = "UP"
                message = ""
            else:
                message = f"HTTP status {response.status_code}"

        except httpx.HTTPError as exc:
            response_time = round(
                (time.perf_counter() - started) * 1000, 2
            )
            message = f"Connection check failed: {type(exc).__name__}"

        monitor.last_status = status
        monitor.last_checked_at = utc_now()
        monitor.response_time_ms = response_time

        open_incident = db.scalars(
            select(Incident).where(
                Incident.monitor_id == monitor_id,
                Incident.resolved_at.is_(None),
            )
        ).first()

        if status == "DOWN" and open_incident is None:
            db.add(
                Incident(
                    monitor_id=monitor_id,
                    message=message,
                    started_at=utc_now(),
                )
            )
            logger.warning("Incident opened for monitor %s", monitor_id)

        elif status == "UP" and open_incident is not None:
            open_incident.resolved_at = utc_now()
            logger.info("Incident resolved for monitor %s", monitor_id)

        db.commit()

    except Exception:
        db.rollback()
        logger.exception("Monitoring check failed for ID %s", monitor_id)
    finally:
        db.close()


def check_all_monitors():
    with SessionLocal() as db:
        ids = list(
            db.scalars(
                select(Monitor.id).where(Monitor.is_active.is_(True))
            ).all()
        )

    for monitor_id in ids:
        check_monitor(monitor_id)


scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    check_all_monitors,
    "interval",
    seconds=60,
    id="monitor_checks",
    max_instances=1,
    coalesce=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    logger.info("CloudOps monitoring scheduler started")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="CloudOps Uptime & Incident Platform",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------- API endpoints ----------

@app.get("/")
def home():
    return {
        "message": "CloudOps monitoring platform is running",
        "docs": "/docs",
    }


@app.post("/monitors", status_code=201)
def create_monitor(
    payload: MonitorCreate,
    db: Session = Depends(get_db),
):
    validate_target_url(payload.url)

    monitor = Monitor(
        name=payload.name,
        url=payload.url,
        is_active=True,
        last_status="UNKNOWN",
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    # Perform the first check immediately.
    check_monitor(monitor.id)
    db.refresh(monitor)

    return monitor


@app.get("/monitors")
def list_monitors(db: Session = Depends(get_db)):
    return db.scalars(select(Monitor).order_by(Monitor.id)).all()


@app.post("/monitors/{monitor_id}/check")
def run_check(
    monitor_id: int,
    db: Session = Depends(get_db),
):
    monitor = db.get(Monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    check_monitor(monitor_id)
    db.refresh(monitor)
    return monitor


@app.get("/incidents")
def list_incidents(db: Session = Depends(get_db)):
    return db.scalars(
        select(Incident).order_by(Incident.started_at.desc())
    ).all()


@app.get("/status")
def service_status(db: Session = Depends(get_db)):
    monitors = db.scalars(select(Monitor)).all()
    active = [m for m in monitors if m.is_active]

    if not active:
        overall = "NO_MONITORS"
    elif any(m.last_status == "DOWN" for m in active):
        overall = "DEGRADED"
    elif any(m.last_status == "UNKNOWN" for m in active):
        overall = "UNKNOWN"
    else:
        overall = "OPERATIONAL"

    return {
        "status": overall,
        "total_monitors": len(active),
        "up": sum(m.last_status == "UP" for m in active),
        "down": sum(m.last_status == "DOWN" for m in active),
    }
