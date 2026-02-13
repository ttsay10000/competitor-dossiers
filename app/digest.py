import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import get_session
from .models import Competitor, Event


SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2}

# Simple check: at least one @ and a dot in the local part or domain
def _is_valid_email(s: str) -> bool:
    s = (s or "").strip()
    if not s or "@" not in s:
        return False
    local, _, domain = s.partition("@")
    return bool(local and domain and "." in domain)


def build_weekly_digest(days: int = 7, per_competitor: int = 3) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        lines: list[str] = []
        lines.append(f"Weekly Digest (last {days} days) — generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
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


def send_weekly_digest(to_emails: list[str], days: int = 7, per_competitor: int = 3) -> tuple[bool, str]:
    """
    Send the Summary report (rollup summary) to the given addresses via SMTP.
    Sends both plain text and HTML (with hyperlinked news) as multipart/alternative.
    Returns (success, message). Uses settings.smtp_* and settings.digest_from_email.
    """
    if not settings.digest_send_enabled:
        return False, "Email send is not configured (set SMTP_* and DIGEST_FROM_EMAIL)."
    to_emails = [e.strip() for e in to_emails if _is_valid_email(e.strip())]
    if not to_emails:
        return False, "No valid email addresses provided."
    from .routes.dossier import get_rollup_summary
    from .executive_summary import format_rollup_summary_for_display
    with get_session() as session:
        rollup_text, _ = get_rollup_summary(session)
    body = rollup_text if rollup_text else "No summary report available. Generate executive summaries for your competitors first."
    subject = f"Competitor Signals — Summary report ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.digest_from_email
    msg["To"] = ", ".join(to_emails)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    import html as html_module
    html_body = format_rollup_summary_for_display(rollup_text) if rollup_text else html_module.escape(body)
    html_wrapped = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body style="font-family: sans-serif; font-size: 14px; line-height: 1.5;">
<div>{html_body}</div>
</body></html>"""
    msg.attach(MIMEText(html_wrapped, "html", "utf-8"))
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_use_tls:
                server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.digest_from_email, to_emails, msg.as_string())
        return True, f"Sent to {', '.join(to_emails)}."
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    print_weekly_digest()
