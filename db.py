import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
url = os.getenv("DATABASE_URL", "sqlite:///./data.db")
if url.startswith("postgres://"): url = url.replace("postgres://", "postgresql+psycopg2://", 1)
elif url.startswith("postgresql://"): url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
engine = create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
class Base(DeclarativeBase): pass
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()
