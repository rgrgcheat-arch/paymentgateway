# Entry point: uvicorn main:app. If the real app fails to import, show the error in the browser (not just Render logs).
try:
    from core import app
except Exception:
    import traceback
    _tb = traceback.format_exc(); print(_tb, flush=True)
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    app = FastAPI()
    @app.get("/{path:path}")
    def _err(path: str): return PlainTextResponse("IMPORT ERROR:\n\n" + _tb, status_code=500)
