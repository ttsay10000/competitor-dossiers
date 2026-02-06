from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine
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
