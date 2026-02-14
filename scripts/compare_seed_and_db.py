#!/usr/bin/env python3
"""
Compare seed_data.json (file) vs database (competitors + sources).

Use this to verify:
- Export seed (DB → file) wrote what you expect
- Run seed (file → DB) would change the DB as expected
- Which side has newer or different data

Requires DATABASE_URL. Run from project root:
  python scripts/compare_seed_and_db.py
  # or with venv:
  .venv/bin/python scripts/compare_seed_and_db.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so DATABASE_URL (and optionally EXTERNAL) are set
_env_file = ROOT / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"").replace("\\n", "\n")
            if key:
                os.environ.setdefault(key, value)
# Use external URL for DB when comparing from local (internal host unreachable)
_export_url = os.environ.get("DATABASE_URL_EXTERNAL") or os.environ.get("DATABASE_URL")
if _export_url:
    os.environ["DATABASE_URL_INTERNAL"] = _export_url
    os.environ["DATABASE_URL"] = _export_url


def _normalize_source(s: dict) -> dict:
    """Normalize a source dict for comparison (order-independent, consistent types)."""
    out = {
        "channel": (s.get("channel") or "").strip(),
        "url": (s.get("url") or "").strip(),
        "confidence": (s.get("confidence") or "high").strip() if s.get("confidence") else "high",
    }
    if s.get("js_required"):
        out["js_required"] = True
    if s.get("use_sitemap_first"):
        out["use_sitemap_first"] = True
    if s.get("extra_options") is not None:
        # Sort keys so diff is stable
        out["extra_options"] = dict(sorted((s.get("extra_options") or {}).items()))
    return out


def _sources_match(file_sources: list, db_sources: list) -> tuple[bool, str]:
    """Compare two lists of normalized sources. Return (match, message)."""
    if len(file_sources) != len(db_sources):
        return False, f"count: file={len(file_sources)} db={len(db_sources)}"
    file_by_key = {}
    for i, src in enumerate(file_sources):
        ch = src.get("channel") or ""
        # For social, include platform so we don't collapse Twitter vs LinkedIn
        key = (ch, (src.get("extra_options") or {}).get("platform") if ch == "social" else None, i)
        file_by_key[key] = src
    db_by_key = {}
    for i, src in enumerate(db_sources):
        ch = src.get("channel") or ""
        key = (ch, (src.get("extra_options") or {}).get("platform") if ch == "social" else None, i)
        db_by_key[key] = src
    if set(file_by_key) != set(db_by_key):
        return False, "channel/platform set differs"
    for k in file_by_key:
        if file_by_key[k] != db_by_key[k]:
            return False, f"content differs for {k}"
    return True, "ok"


def main() -> int:
    # Load file
    path = ROOT / "seed_data.json"
    if not path.exists():
        print("ERROR: seed_data.json not found", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: seed_data.json invalid JSON: {e}", file=sys.stderr)
        return 1
    file_competitors = data.get("competitors", data) if isinstance(data, dict) else data
    if not isinstance(file_competitors, list):
        print("ERROR: seed_data.json has no competitors list", file=sys.stderr)
        return 1

    file_by_name = {}
    for c in file_competitors:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        raw = c.get("sources") or c.get("source") or []
        if isinstance(raw, dict):
            raw = [raw]
        sources = [_normalize_source(s) for s in raw if isinstance(s, dict) and s.get("channel") and s.get("url")]
        file_by_name[name] = {"primary_domain": c.get("primary_domain"), "sources": sources}

    # Load DB (optional if DATABASE_URL missing: report file only)
    if not os.getenv("DATABASE_URL"):
        print("NOTE: DATABASE_URL not set. Showing file contents only (no DB comparison).", file=sys.stderr)
        print()
        print("=" * 70)
        print("SEED FILE CONTENTS (seed_data.json)")
        print("=" * 70)
        for name in sorted(file_by_name):
            rec = file_by_name[name]
            print(f"  {name}: {len(rec['sources'])} sources")
        print("=" * 70)
        print("Set DATABASE_URL and re-run to compare file vs DB.")
        return 0
    try:
        from app.db import get_session
        from app.models import Competitor
        from sqlalchemy.orm import selectinload
    except Exception as e:
        print(f"ERROR: import failed: {e}", file=sys.stderr)
        return 1

    try:
        with get_session() as session:
            db_competitors = (
                session.query(Competitor)
                .options(selectinload(Competitor.source_endpoints))
                .order_by(Competitor.name.asc())
                .all()
            )
            db_by_name = {}
            for c in db_competitors:
                name = (c.name or "").strip()
                if not name:
                    continue
                endpoints = sorted(c.source_endpoints or [], key=lambda e: (e.channel, e.id or 0, e.url or ""))
                sources = []
                for e in endpoints:
                    s = {"channel": e.channel, "url": (e.url or "").strip(), "confidence": (e.confidence or "high").strip()}
                    if e.js_required:
                        s["js_required"] = True
                    if e.use_sitemap_first:
                        s["use_sitemap_first"] = True
                    if e.extra_options is not None:
                        s["extra_options"] = dict(sorted(e.extra_options.items()))
                    sources.append(s)
                db_by_name[name] = {"primary_domain": c.primary_domain, "sources": sources}
    except Exception as e:
        print(f"ERROR: DB query failed: {e}", file=sys.stderr)
        return 1

    # Report
    file_names = set(file_by_name)
    db_names = set(db_by_name)
    only_file = file_names - db_names
    only_db = db_names - file_names
    common = file_names & db_names

    print("=" * 70)
    print("SEED vs DB COMPARISON")
    print("=" * 70)
    print(f"  File: {path}")
    print(f"  DB:   competitors + source_endpoints (DATABASE_URL)")
    print()
    print(f"  In file: {len(file_names)} competitors. In DB: {len(db_names)} competitors.")
    print()

    if only_file:
        print("  ONLY IN FILE (will be added/updated in DB on Run seed):")
        for n in sorted(only_file):
            print(f"    - {n}")
        print()

    if only_db:
        print("  ONLY IN DB (not in file; Export seed would add them to file):")
        for n in sorted(only_db):
            print(f"    - {n}")
        print()

    if common:
        print("  IN BOTH (comparing sources):")
        all_match = True
        for n in sorted(common):
            f = file_by_name[n]
            d = db_by_name[n]
            match, msg = _sources_match(f["sources"], d["sources"])
            if match:
                print(f"    {n}: match ({len(f['sources'])} sources)")
            else:
                all_match = False
                print(f"    {n}: DIFFER — {msg}")
                print(f"      file: {len(f['sources'])} sources")
                print(f"      db:   {len(d['sources'])} sources")
        if not all_match:
            print()
            print("  To make file = DB: click Export seed (or run: python -m app.cli --export-seed)")
            print("  To make DB = file: click Run seed (or run: python -m app.cli --seed)")
    print()
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
