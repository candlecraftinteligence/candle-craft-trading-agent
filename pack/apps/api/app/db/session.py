from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.settings import load_settings

_engine = None
_factory: sessionmaker[Session] | None = None
_url: str | None = None


def reset_engine() -> None:
    global _engine, _factory, _url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None
    _url = None


def get_engine():
    global _engine, _factory, _url
    url = load_settings().database_url
    if _engine is None or _url != url:
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(url, pool_pre_ping=True)
        _factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        _url = url
    return _engine


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _factory is not None
    return _factory


@contextmanager
def session_scope() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
