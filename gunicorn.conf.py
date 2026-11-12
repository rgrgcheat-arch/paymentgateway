# Read automatically by gunicorn. Makes "gunicorn app:app ..." run the FastAPI app correctly
# (FastAPI is ASGI, so it needs the uvicorn worker). Any --workers/--threads flags are tolerated.
import os
worker_class = "uvicorn.workers.UvicornWorker"
workers = 1                      # one process: background worker + keep-alive thread live inside it
bind = "0.0.0.0:" + os.getenv("PORT", "10000")
timeout = 120
graceful_timeout = 30
keepalive = 5
