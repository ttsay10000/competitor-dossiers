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
        # Treat unset or empty as enabled (Dockerfile sets ENV PLAYWRIGHT_ENABLED=true). Only "0"/"false"/"no" disable.
        _pw = (os.getenv("PLAYWRIGHT_ENABLED") or "true").strip().lower()
        self.playwright_enabled = _pw not in ("0", "false", "no")
        # OpenAI: set OPENAI_API_KEY in env (or .env). Used by executive summary, location cleanup, press summarization, asset LLM extraction.
        raw = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"\'')
        self.openai_api_key = raw.replace("\n", "").replace("\r", "").strip()
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
        # Google Places API for reviews channel. Set GOOGLE_PLACES_API_KEY in env.
        self.google_places_api_key = (os.getenv("GOOGLE_PLACES_API_KEY") or "").strip().strip("'\"")
        # Twitter RSS bridge (e.g. Nitter instance) to turn profile URL into RSS. Example: https://nitter.example.com
        self.twitter_rss_bridge_base = (os.getenv("TWITTER_RSS_BRIDGE_BASE_URL") or "").strip().rstrip("/") or None
        # LinkedIn: path to saved Playwright storage state from a logged-in session.
        # Run: python3 -m scripts.save_linkedin_session  (then log in in the browser)
        self.linkedin_storage_state_path = (os.getenv("LINKEDIN_STORAGE_STATE_PATH") or "").strip().rstrip("/") or None
        # Email report (digest) send: optional SMTP. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, DIGEST_FROM_EMAIL to enable "Send" button.
        self.smtp_host = (os.getenv("SMTP_HOST") or "").strip() or None
        self.smtp_port = int((os.getenv("SMTP_PORT") or "587").strip())
        self.smtp_user = (os.getenv("SMTP_USER") or "").strip() or None
        self.smtp_password = (os.getenv("SMTP_PASSWORD") or "").strip() or None
        self.smtp_use_tls = (os.getenv("SMTP_USE_TLS", "true").strip().lower() not in ("0", "false", "no"))
        self.digest_from_email = (os.getenv("DIGEST_FROM_EMAIL") or "").strip() or None

    @property
    def digest_send_enabled(self) -> bool:
        """True if SMTP is configured enough to send the digest email."""
        return bool(
            self.smtp_host
            and self.digest_from_email
            and self.smtp_user
            and self.smtp_password
        )


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
