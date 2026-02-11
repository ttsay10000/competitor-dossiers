import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .routes import competitors, feed, runs, dossier

app = FastAPI(title="Competitor Signals")


@app.on_event("startup")
def _log_playwright_status():
    from .config import settings
    pw_enabled = getattr(settings, "playwright_enabled", False)
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        pw_installed = True
    except ImportError:
        pw_installed = False
    msg = (
        f"[startup] PLAYWRIGHT_ENABLED={pw_enabled!s} (from env), playwright_installed={pw_installed!s}. "
        "Set PLAYWRIGHT_ENABLED=true in Render Dashboard if disabled."
    )
    print(msg, flush=True)
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()

# Paths relative to this file so they work on Render regardless of cwd
_app_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_app_dir / "templates"))
templates.env.globals["utcnow"] = lambda: datetime.now(timezone.utc)
app.state.templates = templates

app.include_router(competitors.router)
app.include_router(feed.router)
app.include_router(runs.router)
app.include_router(dossier.router)

app.mount("/static", StaticFiles(directory=str(_app_dir / "static")), name="static")


@app.get("/health")
def health():
    from .config import settings

    return {
        "status": "ok",
        "version": settings.version,
        "playwright_enabled": getattr(settings, "playwright_enabled", False),
    }


@app.get("/health/playwright")
def health_playwright():
    """Probe: try to launch Chromium once. Use to verify Playwright works on Render (clear cache & deploy if this fails)."""
    from .config import settings

    if not getattr(settings, "playwright_enabled", False):
        return {"playwright_launch": "disabled", "detail": "PLAYWRIGHT_ENABLED is not true"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"playwright_launch": "error", "detail": "playwright not installed"}
    try:
        from .collectors.http import CHROMIUM_LAUNCH_ARGS
    except ImportError:
        CHROMIUM_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
            browser.close()
        return {"playwright_launch": "ok"}
    except Exception as e:
        return {"playwright_launch": "error", "detail": str(e)}
