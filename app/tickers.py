from typing import Optional


# Mapping from competitor display name to public-market ticker symbol.
# Extend this dictionary as you add new public competitors, e.g.:
# "Airbnb": "ABNB",
COMPETITOR_TICKERS: dict[str, str] = {
    # "Airbnb": "ABNB",
}


def get_competitor_ticker(name: str) -> Optional[str]:
    """
    Return the ticker symbol for a competitor, if configured.

    The lookup is case-sensitive on the exact name string; adjust COMPETITOR_TICKERS
    to match how competitors are stored in the database (e.g. \"Airbnb\", \"Marriott\" etc.).
    """
    return COMPETITOR_TICKERS.get((name or "").strip())

