# Review: logic flow and UI containers for new signals

Quick audit of where new signals appear on the site and logic-flow checks.

---

## Where new signal containers live on the site

| Signal | Where it appears | Context / source |
|--------|------------------|-------------------|
| **Executive summary** | Dossier: top card | All signals feed into `_build_context_text` → LLM. News, asset, talent, digital footprint (events) are precedent; social events and review block (when significant) are secondary. |
| **Top news** | Dossier: card below executive summary | `top_news` / `press_90d` from `build_dossier_context`. |
| **Property reviews** | Dossier: “Property reviews” card | `reviews_minimal` from latest reviews snapshot; built in `build_dossier_context` from `latest_reviews.structured_json.properties` (display_name, line=sentiment_summary, trend). |
| **Social (Twitter & LinkedIn)** | Dossier: “Social (Twitter & LinkedIn)” card | `social_posts` from latest social snapshot; built in `build_dossier_context` from `latest_social.structured_json.posts`. Template shows up to 20 posts; “Executive-relevant” when `p.relevance == 'executive'`. |
| **Digital footprint** | No dedicated card | Shown as **events** only: `narrative.homepage_updated`, `narrative.coming_soon` appear in (1) “Recent events (past 90 days)” at bottom of dossier and (2) Feed, and (3) in the executive summary text as “Events detected this week” with label `[digital footprint]`. |
| **Recent events (past 90 days)** | Dossier: bottom card | `events` from `build_dossier_context` (all event types: talent, asset, partner, capital, narrative.*, public_record). Digital footprint and social events appear here. |
| **Data / Status (T A P W S R)** | Competitors list: “Data / Status” column | `display_channels` and `channel_letters` passed from `competitors_list`; template loops `display_channels` and uses `channel_letters.get(ch, ch[0]|upper)`. Homepage=W, social=S, reviews=R. |
| **Run-now checkboxes** | Dossier (“Populate now”) and competitor added/edit | Channels: talent, asset, press, reviews, social. Homepage is not a checkbox; runner runs homepage from `primary_domain` when running all channels. |

So: **reviews** and **social** have dedicated cards on the dossier; **digital footprint** is surfaced only via events (and executive summary text). No missing containers.

---

## Logic flow checks

1. **Executive summary**
   - `build_dossier_context` fills `events_this_week`, `reviews_minimal`, etc. No filter on events by severity here.
   - `_build_context_text` receives that context; builds “Events detected this week” from **all** `events_this_week`, sorted by precedent (talent, asset, partner, capital, digital footprint) then secondary (social), then by date (newest first). Review block only when `_is_significant_review(r)` for at least one row.
   - `_is_significant_review`: `trend` lowercased in (`"declining"`, `"negative"`, `"down"`) or sentiment `line` contains negative markers. Reviews snapshot uses trend `"POSITIVE"` / `"NEGATIVE"` / `"NEUTRAL"`, so `"NEGATIVE".lower()` matches. OK.

2. **Event priority**
   - `_event_priority_rank`: returns 0 for precedent (talent.*, asset.*, partner.*, capital.*, narrative.homepage_updated, narrative.coming_soon, narrative.priority_shift, public_record.*), 1 for narrative.social_signal and narrative.review*. Sort: by date desc, then by rank (stable), so precedent events first, then social, newest first within each group. OK.

3. **Competitors list**
   - Only `competitors_list` renders `competitors.html`; it always passes `display_channels` and `channel_letters`. Template uses `{% for ch in display_channels %}` and `channel_letters.get(ch, ch[0]|upper)`. No other route renders this template. OK.

4. **Reviews minimal**
   - Built from `latest_reviews.structured_json.properties`; each item has `display_name`, `line` (sentiment_summary), `trend`. Template uses `r.display_name`, `r.line`, `r.trend`. Executive summary uses same keys. OK.

5. **Social posts**
   - Built from `latest_social.structured_json.posts`; template expects `p.platform`, `p.relevance`, `p.text`/`p.title`, `p.url`. Enrichment sets `relevance` to `"executive"` or `"promotion"` (lowercase). Template checks `p.relevance == 'executive'`. OK.

No bugs found in the above flow. Ready to commit when you direct.
