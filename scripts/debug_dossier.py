#!/usr/bin/env python3
# Run: python scripts/debug_dossier.py [competitor_id]
# Reproduces dossier load locally and prints any exception (from build_dossier_context or template).
# Requires: .env with DATABASE_URL, and .venv with deps.

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    competitor_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"Building dossier context for competitor_id={competitor_id} ...")
    try:
        from app.db import get_session, get_last_refreshed
        from app.models import Competitor
        from app.routes.dossier import build_dossier_context
        with get_session() as session:
            context = build_dossier_context(session, competitor_id)
            context["last_refreshed"] = get_last_refreshed(session)
            all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
            context["nav_competitors"] = [{"id": c.id, "name": c.name} for c in all_competitors]
        print("build_dossier_context OK.")
        if "error" in context:
            print("Context error:", context["error"])
            return
        from starlette.templating import Jinja2Templates
        from datetime import datetime, timezone
        templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
        templates.env.globals["utcnow"] = lambda: datetime.now(timezone.utc)
        class FakeRequest:
            pass
        req = FakeRequest()
        req.base_url = "http://localhost:8000"
        # Sync render to catch template errors
        template = templates.env.get_template("dossier.html")
        body = template.render(request=req, **context)
        print(f"Template render OK, body length={len(body)}.")
    except Exception as e:
        import traceback
        print("FAILED:", type(e).__name__, str(e))
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
