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
        self.playwright_enabled = os.getenv("PLAYWRIGHT_ENABLED", "false").lower() in {"1", "true", "yes"}
        self.version = os.getenv("APP_VERSION", "0.1.0")


settings = Settings()
