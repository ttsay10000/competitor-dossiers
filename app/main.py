from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .routes import competitors, feed, runs, dossier

app = FastAPI(title="Competitor Signals")

# Paths relative to this file so they work on Render regardless of cwd
_app_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_app_dir / "templates"))
templates.env.globals["utcnow"] = datetime.utcnow
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
