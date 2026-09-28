import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# Default is SQLite so the app runs anywhere with zero setup.
# For PostgreSQL: DATABASE_URL=postgresql+psycopg2://rnb:<password>@localhost:5432/rnb
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./rnb_assets.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
