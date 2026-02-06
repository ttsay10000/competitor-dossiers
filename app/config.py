import os


class Settings:
    def __init__(self) -> None:
        url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/competitor_signals")
        # Render and some hosts give postgres://; SQLAlchemy/psycopg2 expect postgresql://
        if url and url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        self.database_url = url
        self.playwright_enabled = os.getenv("PLAYWRIGHT_ENABLED", "false").lower() in {"1", "true", "yes"}
        self.version = os.getenv("APP_VERSION", "0.1.0")


settings = Settings()
