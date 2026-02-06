from datetime import datetime, timedelta

from .db import get_session
from .models import Competitor, Event


SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2}


def build_weekly_digest(days: int = 7, per_competitor: int = 3) -> str:
    cutoff = datetime.utcnow() - timedelta(days=days)

    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        lines: list[str] = []
        lines.append(f"Weekly Digest (last {days} days) — generated {datetime.utcnow().strftime('%Y-%m-%d')}")
        lines.append("")

        for competitor in competitors:
            events = (
                session.query(Event)
                .filter(Event.competitor_id == competitor.id, Event.detected_at >= cutoff)
                .all()
            )
            if not events:
                continue

            events.sort(key=lambda e: (SEVERITY_ORDER.get(e.severity, 9), e.detected_at), reverse=False)
            selected = events[:per_competitor]

            lines.append(f"{competitor.name}")
            for event in selected:
                lines.append(f"- [{event.severity.upper()}] {event.title}")
                lines.append(f"  Why it matters: {event.why_it_matters or event.summary}")
            lines.append("")

    return "\n".join(lines).strip()


def print_weekly_digest(days: int = 7, per_competitor: int = 3) -> None:
    digest = build_weekly_digest(days=days, per_competitor=per_competitor)
    print(digest)


if __name__ == "__main__":
    print_weekly_digest()
