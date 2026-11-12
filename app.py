# Compatibility entry point: "gunicorn app:app" and "uvicorn app:app" and "uvicorn main:app" ALL work.
from main import app  # noqa: F401
