# SIGNALS

## Event Taxonomy (MVP)
Top-level categories: talent, asset, partner, capital, narrative, public_record
Event types:
- talent.senior_hire_or_role_posted
- talent.new_capability
- talent.hiring_surge
- asset.new_market
- asset.market_exit (requires confirmation)
- asset.pipeline_signal
- partner.major_partnership
- partner.partnership_surge
- capital.fundraise_or_restructuring
- narrative.priority_shift (only when explicit)
- narrative.homepage_updated (digital footprint change)
- narrative.social_signal (executive-relevant company post from Twitter/LinkedIn)
- public_record.filing (trademark, regulatory, etc.)

## Capability Buckets (Talent)
- ai_data: ai, ml, machine learning, data, analytics, automation
- strategy_finance: strategy, corp dev, bizops, strategic finance, fp&a
- partnerships: partnerships, enterprise, institutional, alliances, bd
- real_estate: acquisitions, development, portfolio, asset management

## Severity Rules
HIGH if any:
- VP/Head/C-level role or senior strategic role (AI/data, strategy/finance, partnerships, portfolio/real estate)
- New capability category appears for that competitor (first observed)
- Hiring surge in a strategic function (>=5 roles in same capability bucket within 30 days)
- New market / confirmed market exit
- Major partnership (even if non-exclusive) OR partnership surge (>=3 partnership announcements in same vertical within 60 days)
- Fundraise/restructuring/layoffs

MED:
- 2–4 strategic roles in 30 days
- Single partnership that isn’t clearly major
- New properties in existing markets

LOW:
- Routine hiring, minor PR; generally hidden by default

## Persistence Gates & Dedup
- Market exits require 2 consecutive runs showing removal.
- Deduplicate related deltas into single events (e.g., hiring surge).
- Ignore cosmetic edits, typos, reposted jobs, duplicate press.

## Press Classification (Rules-First)
- partner: partnership, alliance, distribution, channel, platform
- capital: fundraise, debt, restructuring, layoffs, recap
- narrative: explicit priority shift language (strategy change, focus shift)
- Unclassified items are stored but not alerted.
