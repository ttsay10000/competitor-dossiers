import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .routes import competitors, feed, runs, dossier

app = FastAPI(title="Competitor Signals")


@app.exception_handler(Exception)
async def debug_exception_handler(request, exc):
    """Return traceback in 500 response so we can see the error on Render. Remove after fixing."""
    body = f"{exc!r}\n\n{traceback.format_exc()}"
    import sys
    print(body, file=sys.stderr, flush=True)
    return PlainTextResponse(body, status_code=500, media_type="text/plain; charset=utf-8")

# Paths relative to this file so they work on Render regardless of cwd
_app_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_app_dir / "templates"))
app.state.templates = templates

app.include_router(competitors.router)
app.include_router(feed.router)
app.include_router(runs.router)
app.include_router(dossier.router)

app.mount("/static", StaticFiles(directory=str(_app_dir / "static")), name="static")


@app.get("/health")
def health():
    from .config import settings

    return {"status": "ok", "version": settings.version}
