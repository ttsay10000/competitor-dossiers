"""
App configuration. All OpenAI usage must go through get_openai_client() so a single
OPENAI_API_KEY env var is used for every LLM call (executive summary, location cleanup,
press summarization, asset extraction, talent/press enrichment).
"""
import os


class Settings:
    def __init__(self) -> None:
        url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/competitor_signals")
        # Normalize to postgresql:// then use psycopg driver (Python 3.13 compatible)
        if url and url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        # Render Postgres (and many cloud DBs) require SSL; add sslmode if URL looks like Render and has no params
        if url and "dpg-" in url and "?" not in url:
            url = url.rstrip("/") + "?sslmode=require"
        if url and ("postgresql://" in url or "postgresql+psycopg://" in url) and not url.startswith("postgresql+psycopg://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.database_url = url
        self.playwright_enabled = os.getenv("PLAYWRIGHT_ENABLED", "true").lower() in {"1", "true", "yes"}
        # OpenAI: set OPENAI_API_KEY in env (or .env). Used by executive summary, location cleanup, press summarization, asset LLM extraction.
        self.openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        self.version = os.getenv("APP_VERSION", "0.1.0")
        # Press: only user endpoints + PR Newswire + Google News (90d). BI / Yahoo / CNBC disabled for now.
        self.press_enable_business_insider = os.getenv("PRESS_ENABLE_BUSINESS_INSIDER", "false").lower() in {"1", "true", "yes"}
        self.press_enable_yahoo_finance = os.getenv("PRESS_ENABLE_YAHOO_FINANCE", "false").lower() in {"1", "true", "yes"}
        self.press_enable_cnbc = os.getenv("PRESS_ENABLE_CNBC", "false").lower() in {"1", "true", "yes"}
        self.press_enable_google_news = os.getenv("PRESS_ENABLE_GOOGLE_NEWS", "true").lower() in {"1", "true", "yes"}
        # Global caps to keep LLM + HTTP work manageable.
        self.press_max_raw_items_per_competitor = int(os.getenv("PRESS_MAX_RAW_ITEMS_PER_COMPETITOR", "120") or "120")
        self.press_max_items_per_source = int(os.getenv("PRESS_MAX_ITEMS_PER_SOURCE", "30") or "30")
        self.press_max_articles_to_summarize = int(os.getenv("PRESS_MAX_ARTICLES_TO_SUMMARIZE", "40") or "40")
        # Seed/baseline mode: when true, the first run for each channel
        # will persist a baseline snapshot but skip creating events so that
        # subsequent scheduled runs only emit deltas vs this baseline.
        self.seed_mode = os.getenv("SEED_MODE", "false").lower() in {"1", "true", "yes"}


settings = Settings()


def get_openai_client():  # -> Optional[OpenAI]; OpenAI is optional dependency
    """Return an OpenAI client using settings.openai_api_key, or None if key unset or openai not installed."""
    if not settings.openai_api_key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=settings.openai_api_key)
    except ImportError:
        return None
