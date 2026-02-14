from pathlib import Path
import json
import re
from typing import Any, Optional

from .db import get_export_session, get_session
from .models import Competitor, CompetitorReviewProperty, SourceEndpoint


def _seed_py_path() -> Path:
    """Path to seed.py (this module)."""
    return Path(__file__).resolve()


def _seed_data_path() -> Path:
    """Path to seed_data.json in project root (committed; survives redeploys)."""
    return Path(__file__).resolve().parent.parent / "seed_data.json"


def _sync_seed_fallback_to_py(competitors: list[dict[str, Any]]) -> bool:
    """
    Update SEED_COMPETITORS in seed.py to match the given competitors list.
    Keeps fallback in sync when Export runs (so deploy without seed_data.json still gets UI-added competitors).
    Returns True if seed.py was updated, False on error or no change.
    """
    try:
        seed_path = _seed_py_path()
        content = seed_path.read_text()
        raw = json.dumps(competitors, indent=4)
        py_literal = re.sub(r"\btrue\b", "True", raw)
        py_literal = re.sub(r"\bfalse\b", "False", py_literal)
        py_literal = re.sub(r"\bnull\b", "None", py_literal)
        new_block = "SEED_COMPETITORS = " + py_literal
        # Match only at line start (avoid matching inside comments in this function)
        match = re.search(r"^SEED_COMPETITORS\s*=\s*\[", content, re.MULTILINE)
        if not match:
            return False
        start = match.start()
        brace_start = match.end() - 1
        depth = 1
        i = brace_start + 1
        while i < len(content) and depth > 0:
            if content[i] == "[":
                depth += 1
            elif content[i] == "]":
                depth -= 1
            i += 1
        end = i
        new_content = content[:start] + new_block + content[end:]
        if new_content == content:
            return False
        seed_path.write_text(new_content)
        return True
    except Exception:
        return False


def load_seed_competitors() -> list[dict[str, Any]]:
    """Load competitor list from seed_data.json. Falls back to SEED_COMPETITORS if file missing."""
    path = _seed_data_path()
    if not path.exists():
        return SEED_COMPETITORS
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return SEED_COMPETITORS
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "competitors" in data:
        return data["competitors"]
    return SEED_COMPETITORS


SEED_COMPETITORS = [
    {
        "name": "AKA",
        "primary_domain": "stayaka.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.stayaka.com/",
                "confidence": "high"
            },
            {
                "channel": "press",
                "url": "https://stayaka.com",
                "confidence": "high",
                "extra_options": {
                    "google_news_search_phrases": [
                        "furnished rentals"
                    ],
                    "press_search_name": "AKA"
                }
            },
            {
                "channel": "talent",
                "url": "https://www.indeed.com/cmp/Aka-Hotels-Hotel-Residences/jobs#cmp-skip-header-desktop",
                "confidence": "high"
            }
        ]
    },
    {
        "name": "AvantStay",
        "primary_domain": "avantstay.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://avantstay.com/search",
                "confidence": "low",
                "js_required": True,
                "use_sitemap_first": True,
                "extra_options": {
                    "llm_extract": True,
                    "strategy_chain": [
                        "sitemap_first",
                        "html"
                    ],
                    "min_properties_accept": 5
                }
            },
            {
                "channel": "press",
                "url": "https://avantstay.com/blog/",
                "confidence": "high"
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/avantstay/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://careers.kula.ai/avantstay",
                "confidence": "high"
            }
        ]
    },
    {
        "name": "Blueground",
        "primary_domain": "theblueground.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.theblueground.com/destinations",
                "confidence": "medium",
                "extra_options": {
                    "strategy": "blueground_destinations",
                    "max_destinations": None
                }
            },
            {
                "channel": "press",
                "url": "https://www.theblueground.com/blog",
                "confidence": "medium"
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/blueground-co/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://www.theblueground.com/careers",
                "confidence": "medium",
                "js_required": True
            }
        ]
    },
    {
        "name": "Kasa Living",
        "primary_domain": "kasa.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://kasa.com/locations",
                "confidence": "high",
                "js_required": True,
                "extra_options": {
                    "strategy": "js_exhaust",
                    "load_more": {
                        "button_text": "Load more",
                        "post_load_wait_ms": 3000,
                        "click_selector": [
                            "a:has-text('Load more')",
                            "button:has-text('Load more')",
                            ":text('Load more')",
                            "button:has-text('Load more')",
                            "button:has-text('Load More')",
                            "a:has-text('Load more')",
                            "a:has-text('Load More')",
                            "button:has-text('View more')",
                            "a:has-text('View more')",
                            "[data-testid='load-more']",
                            "button:has-text('Show more')",
                            "a:has-text('Show more')"
                        ],
                        "stop_when_selector_gone": True,
                        "wait_after_click_ms": 2000,
                        "wait_for_selector_timeout_ms": 10000,
                        "wait_after_gone_ms": 3000,
                        "wait_reappear_attempts": 5,
                        "max_clicks": 200
                    },
                    "llm_extract": True
                }
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/kasa-living/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://job-boards.greenhouse.io/kasaliving",
                "confidence": "high"
            }
        ]
    },
    {
        "name": "Landing",
        "primary_domain": "hellolanding.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.hellolanding.com/locations",
                "confidence": "high",
                "extra_options": {
                    "strategy": "landing_locations"
                }
            },
            {
                "channel": "press",
                "url": "https://www.hellolanding.com/blog",
                "confidence": "medium"
            },
            {
                "channel": "press",
                "url": "https://hellolanding.com",
                "confidence": "high",
                "extra_options": {
                    "google_news_search_phrases": [
                        "furnished rentals"
                    ],
                    "press_search_name": "Landing"
                }
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/hellolanding/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://www.hellolanding.com/p/careers/",
                "confidence": "medium"
            }
        ]
    },
    {
        "name": "Lark",
        "primary_domain": "larkhospitality.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.larkhospitality.com/portfolio/",
                "confidence": "high",
                "js_required": True,
                "extra_options": {
                    "strategy": "js_exhaust",
                    "load_more": {
                        "button_text": "Load more hotels",
                        "post_load_wait_ms": 3000,
                        "click_selector": [
                            "a:has-text('Load more hotels')",
                            "button:has-text('Load more hotels')",
                            ":text('Load more hotels')",
                            "button:has-text('Load more')",
                            "button:has-text('Load More')",
                            "a:has-text('Load more')",
                            "a:has-text('Load More')",
                            "button:has-text('View more')",
                            "a:has-text('View more')",
                            "[data-testid='load-more']",
                            "button:has-text('Show more')",
                            "a:has-text('Show more')"
                        ],
                        "stop_when_selector_gone": True,
                        "wait_after_click_ms": 2000,
                        "wait_for_selector_timeout_ms": 10000,
                        "wait_after_gone_ms": 3000,
                        "wait_reappear_attempts": 5,
                        "max_clicks": 200
                    },
                    "llm_extract": True
                }
            },
            {
                "channel": "press",
                "url": "https://www.larkhospitality.com/press/",
                "confidence": "high",
                "extra_options": {
                    "press_search_name": "Lark Hotels"
                }
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/lark-hotels/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://ats.wizehire.com/career-site/lark-hospitality",
                "confidence": "high"
            }
        ]
    },
    {
        "name": "Placemakr",
        "primary_domain": "placemakr.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.placemakr.com/locations",
                "confidence": "high",
                "extra_options": {
                    "strategy_chain": [
                        "html"
                    ],
                    "min_properties_accept": 1
                }
            },
            {
                "channel": "homepage",
                "url": "https://www.placemakr.com",
                "confidence": "high",
                "extra_options": {
                    "product_paths": [
                        "/locations"
                    ]
                }
            },
            {
                "channel": "press",
                "url": "https://www.placemakr.com/blog",
                "confidence": "high"
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/placemakr/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://jobs.lever.co/placemakr",
                "confidence": "high"
            }
        ]
    },
    {
        "name": "Rove",
        "primary_domain": "rovetravel.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://rovetravel.com/search",
                "confidence": "high",
                "js_required": True,
                "extra_options": {
                    "strategy_chain": [
                        "js_exhaust",
                        "sitemap_first",
                        "html"
                    ],
                    "load_more": {
                        "scroll_window": True,
                        "scroll_wait_sec": 1.5,
                        "max_scrolls": 150
                    }
                }
            },
            {
                "channel": "press",
                "url": "https://rovetravel.com",
                "confidence": "high",
                "extra_options": {
                    "google_news_search_phrases": [
                        "furnished rentals"
                    ],
                    "press_search_name": "Rove"
                }
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/rovetravel/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://jobs.gem.com/rove",
                "confidence": "high"
            }
        ]
    },
    {
        "name": "Vacasa",
        "primary_domain": "vacasa.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.vacasa.com/search?place=/usa/",
                "confidence": "high",
                "use_sitemap_first": True,
                "extra_options": {
                    "strategy_chain": [
                        "sitemap_first",
                        "html"
                    ],
                    "min_properties_accept": 5
                }
            },
            {
                "channel": "press",
                "url": "https://www.vacasa.com/blog",
                "confidence": "medium"
            },
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/vacasa/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {
                    "platform": "linkedin"
                }
            },
            {
                "channel": "talent",
                "url": "https://job-boards.greenhouse.io/vacasa",
                "confidence": "medium"
            }
        ]
    }
]


def upsert_competitor(session, name: str, primary_domain: Optional[str], is_active: bool = True) -> Competitor:
    competitor = session.query(Competitor).filter(Competitor.name == name).first()
    if competitor:
        competitor.primary_domain = primary_domain
        competitor.is_active = is_active
        return competitor
    competitor = Competitor(name=name, primary_domain=primary_domain, is_active=is_active)
    session.add(competitor)
    session.flush()
    return competitor


def upsert_source(
    session,
    competitor_id: int,
    channel: str,
    url: str,
    confidence: str,
    js_required: bool = False,
    use_sitemap_first: bool = False,
    extra_options: Optional[dict] = None,
) -> None:
    # Match by competitor + channel. For channel "social", also match by extra_options.platform so Twitter and LinkedIn both persist.
    q = session.query(SourceEndpoint).filter(
        SourceEndpoint.competitor_id == competitor_id,
        SourceEndpoint.channel == channel,
    )
    if channel == "social" and extra_options and extra_options.get("platform"):
        platform = extra_options.get("platform")
        candidates = q.all()
        existing = next((e for e in candidates if (e.extra_options or {}).get("platform") == platform), None)
    else:
        existing = q.first()
    if existing:
        existing.url = url
        existing.confidence = confidence
        existing.js_required = js_required
        existing.use_sitemap_first = use_sitemap_first
        existing.extra_options = extra_options
        return
    session.add(
        SourceEndpoint(
            competitor_id=competitor_id,
            channel=channel,
            url=url,
            confidence=confidence,
            js_required=js_required,
            use_sitemap_first=use_sitemap_first,
            extra_options=extra_options,
        )
    )


def upsert_review_property(
    session,
    competitor_id: int,
    place_id: str,
    display_name: Optional[str] = None,
) -> None:
    existing = (
        session.query(CompetitorReviewProperty)
        .filter(
            CompetitorReviewProperty.competitor_id == competitor_id,
            CompetitorReviewProperty.place_id == place_id,
        )
        .first()
    )
    if existing:
        existing.display_name = display_name
        return
    session.add(
        CompetitorReviewProperty(
            competitor_id=competitor_id,
            place_id=place_id,
            display_name=display_name,
        )
    )


def run_seed() -> None:
    """Upsert competitors, sources, and review properties from seed_data.json. Never deletes existing DB rows."""
    with get_session() as session:
        for entry in load_seed_competitors():
            competitor = upsert_competitor(
                session,
                entry["name"],
                entry.get("primary_domain"),
                is_active=entry.get("is_active", True),
            )
            # Prefer "sources" (list); allow "source" (single dict) so typos don't leave competitor with no endpoints.
            raw_sources = entry.get("sources") if entry.get("sources") is not None else entry.get("source")
            if isinstance(raw_sources, dict):
                raw_sources = [raw_sources]
            sources = raw_sources if isinstance(raw_sources, list) else []
            for source in sources:
                if not isinstance(source, dict) or not source.get("channel") or not source.get("url"):
                    continue
                upsert_source(
                    session,
                    competitor.id,
                    source["channel"],
                    source["url"],
                    source.get("confidence", "high"),
                    js_required=bool(source.get("js_required")),
                    use_sitemap_first=bool(source.get("use_sitemap_first")),
                    extra_options=source.get("extra_options"),
                )
            for rp in entry.get("review_properties", []):
                place_id = (rp.get("place_id") or "").strip()
                if place_id:
                    upsert_review_property(
                        session,
                        competitor.id,
                        place_id,
                        (rp.get("display_name") or "").strip() or None,
                    )


def export_seed_to_file() -> None:
    """Write current DB competitors and sources to seed_data.json. Uses external DB URL when DATABASE_URL_EXTERNAL is set."""
    with get_export_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        out = []
        for c in competitors:
            endpoints = sorted(c.source_endpoints, key=lambda e: (e.channel, e.id))
            sources = []
            for e in endpoints:
                s: dict[str, Any] = {
                    "channel": e.channel,
                    "url": e.url,
                    "confidence": e.confidence or "high",
                }
                if e.js_required:
                    s["js_required"] = True
                if e.use_sitemap_first:
                    s["use_sitemap_first"] = True
                if e.extra_options:
                    s["extra_options"] = e.extra_options
                sources.append(s)
            review_properties = []
            for rp in getattr(c, "review_properties", []) or []:
                review_properties.append({
                    "place_id": rp.place_id,
                    "display_name": rp.display_name or None,
                })
            row = {
                "name": c.name,
                "primary_domain": c.primary_domain or None,
                "sources": sources,
            }
            if review_properties:
                row["review_properties"] = review_properties
            if not getattr(c, "is_active", True):
                row["is_active"] = False
            out.append(row)
    path = _seed_data_path()
    path.write_text(json.dumps({"competitors": out}, indent=2) + "\n")
    print(f"Wrote {len(out)} competitor(s) to {path}", flush=True)
    if _sync_seed_fallback_to_py(out):
        print("Synced SEED_COMPETITORS fallback in seed.py", flush=True)


if __name__ == "__main__":
    run_seed()
