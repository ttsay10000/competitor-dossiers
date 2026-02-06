# PRD

## Primary User / Success
- User: CEO/Exec/Strategy
- Success: 3–7 meaningful items per week across tracked competitors.
- Each item includes: why it matters + recommended response.
- Bias: prefer fewer alerts (false negatives) over noisy alerts (false positives).

## MVP Competitors
- Placemakr
- AvantStay
- Lark
- Blueground
- Must support adding competitors (extensible, no hardcoding).

## MVP Signal Channels
1. Talent Radar
2. Asset Watch
3. Press/Public Narrative

## Meaningful Change (Rules-First)
Meaningful changes imply shifts in:
- Footprint (new market, market exit, pipeline “coming soon”)
- Capabilities (new senior hires; emergence of new capability areas)
- Partnerships/distribution (including big partnerships even if non-exclusive)
- Capital/risk (fundraise, restructuring, layoffs)
Everything else is filtered as noise (typos, cosmetic edits, reposted jobs, duplicate press).

## Event Taxonomy (MVP)
Top-level categories: talent, asset, partner, capital, narrative
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

## Outputs (MVP)
- /competitors: add/edit competitor + URLs
- /feed: timeline of events with filters by competitor/type/severity (LOW hidden by default)
- Weekly digest (text output first): last 7 days, top 1–3 events per competitor, why-it-matters, suggested response

## Non-Goals
- Real-time alerts
- Extensive benchmarking (pricing, ops KPIs)
- LLM-driven event detection (rules-only gating)
- Broad competitor coverage beyond the initial list
