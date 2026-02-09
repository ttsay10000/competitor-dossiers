from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import settings

DATABASE_URL = settings.database_url

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_last_refreshed(session) -> Optional[datetime]:
    """Latest RunLog.created_at across all channels (for dashboard 'last refreshed')."""
    from .models import RunLog
    row = session.query(RunLog).order_by(RunLog.created_at.desc()).first()
    return row.created_at if row else None


def check_db_connection() -> None:
    """Verify DB is reachable. Raises RuntimeError with a clear message if not (e.g. DATABASE_URL missing or wrong)."""
    from .config import settings
    url = settings.database_url
    if not url or url.strip() == "":
        raise RuntimeError(
            "DATABASE_URL is not set. Set it in your environment or, on Render, link the Postgres instance to this service."
        )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        raise RuntimeError(
            f"Cannot connect to database: {e!s}. "
            "Check DATABASE_URL and that the database is running. "
            "On Render: ensure the cron job has DATABASE_URL (link the Postgres instance or add it to the cron job's environment)."
        ) from e
