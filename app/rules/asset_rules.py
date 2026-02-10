from datetime import datetime, timezone


def build_new_market_event(market: str) -> dict:
    return {
        "category": "asset",
        "type": "asset.new_market",
        "severity": "high",
        "title": f"New market detected: {market}",
        "summary": f"New market appeared in asset footprint: {market}.",
        "why_it_matters": "Indicates footprint expansion into a new market.",
        "evidence": {"market": market},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def build_pipeline_event(prop: dict) -> dict:
    return {
        "category": "asset",
        "type": "asset.pipeline_signal",
        "severity": "med",
        "title": f"Pipeline signal: {prop.get('name')}",
        "summary": "Property listed as coming soon or pipeline.",
        "why_it_matters": "Signals potential near-term supply expansion.",
        "evidence": {"property": prop},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def build_market_exit_event(market: str) -> dict:
    return {
        "category": "asset",
        "type": "asset.market_exit",
        "severity": "high",
        "title": f"Market exit signal: {market}",
        "summary": f"Market appears removed from footprint: {market}.",
        "why_it_matters": "Potential strategic retreat or asset reallocation.",
        "evidence": {"market": market},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
