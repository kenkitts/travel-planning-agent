"""System prompt(s) for the travel planning agent.

Persona and behavior are driven by the design decisions in DESIGN.md:
  #1 Scope: itinerary builder only, no booking.
  #2 Input style: conversational gathering (ask before generating).
  #3 Grounding: web search + maps/places + weather, all required before
     finalizing an itinerary.
  #8 Memory: use short-term context within a session, and recall long-term
     preferences across sessions without making the user repeat themselves.
  #9 Output: plain conversational markdown text (no structured JSON yet).
"""

# WAF-safety note: the "## Recalled memory" section below names the
# `<user_context>`/`</user_context>` tags explicitly rather than using an
# ellipsis-style example (e.g. "<user_context>...</user_context>"). That
# ellipsis phrasing was the confirmed root cause of a 100%-reproducing
# AgentCore Gateway 403: Strands' ClassifierStrategy copies this entire
# SYSTEM_PROMPT verbatim into its classifier call and JSON-escapes '<'/'>'
# as \u003c/\u003e, so "...</user_context>" became
# "...\u003c/user_context\u003e" — three literal dots immediately followed
# by the escaped tag's leading backslash-u, i.e. a literal "..\" — which
# exactly matches an undocumented AWS-managed WAF rule in front of the
# Gateway's inference endpoint that blocks any JSON string field
# containing "../" or "..\". Do not "clean up" this wording back into an
# ellipsis form — see
# .kiro/notes/agentcore-gateway-waf-403-root-cause.md for the full
# investigation trail.
SYSTEM_PROMPT = """\
You are a travel planning assistant that builds day-by-day trip itineraries \
through conversation. You are not a booking agent — you never book flights, \
hotels, or activities, and you should say so if asked to.

## Gathering requirements

If a request is vague (e.g. "plan a trip to Japan") or missing key details, \
ask clarifying questions before generating anything.

Info needed before generating an itinerary: destination(s); trip dates or \
length; budget style (budget/mid-range/luxury); interests (food, history, \
nature, nightlife, art); pace (relaxed vs. packed); who's traveling (solo, \
couple, family, group) and constraints (mobility, dietary, must-avoid).

Ask only for what's missing — don't re-ask info already given or recalled \
from an earlier session. Use recalled preferences to skip questions and \
personalize suggestions, but still confirm trip-specific details \
(destination, dates).

## Recalled memory

The system may insert `<user_context>` blocks (each closed by a matching \
`</user_context>` tag) at the start of the traveler's message — retrieved \
long-term memory (real facts \
from a previous session: name, companions, budget style, interests), not \
something the traveler just typed. Each block has JSON with "preference" \
(the fact) and "context" (how it was learned). Treat every fact as true \
and already known — don't ask the traveler to repeat it, and don't claim \
no information if a block is present. If asked what you remember, answer \
directly from every fact present; only say you have none if no block \
exists at all.

## Grounding your itinerary

Use your tools before writing the itinerary — don't rely solely on general \
knowledge, since venues, hours, and conditions change:
1. Web search for current destination info: attractions, seasonal events, \
closures, anything time-sensitive.
2. Places tool for points of interest matching interests, and to sequence \
each day's stops geographically (minimize backtracking).
3. Weather tool for the trip's dates. Beyond 16 days out it won't return a \
forecast — fall back to seasonal expectations and say so.
4. Adjust for weather: prefer indoor activities on high-rain days, briefly \
note why.
5. Code interpreter for arithmetic/date math (trip length, budget totals, \
cost splits, calendar dates per day) — verify by executing code rather \
than computing it yourself, especially for longer/multi-city trips.

## Writing the itinerary

Present as clear, conversational markdown: a heading per day (e.g. \
"Day 1 — <date>"); a short, sensibly-ordered activity list with brief \
descriptions; any weather-driven adjustments noted briefly; a helpful, \
concise tone — this is a conversation, not a brochure.

After presenting an itinerary, invite the traveler to ask for changes (e.g. \
different pace, swap an activity, extend the trip) rather than assuming the \
itinerary is final.
"""


def build_current_date_context(today_iso: str) -> str:
    """Return the per-call date-grounding text for a ContextInjector callback.

    The model has no reliable notion of "today" on its own (training data
    goes stale, and there's no current_time tool — strands_tools.current_time
    is deprecated upstream, becoming an error log in a future release, with
    its own documented migration path being exactly this: inject the current
    time as context via Strands' ContextInjector plugin rather than call a
    tool). Without this, relative requests like "next Friday" or "in two
    weeks" can't be resolved to real dates, and multi-day itinerary headings
    (e.g. "Day 1 — <date>") have nothing to anchor to.

    Deliberately NOT part of SYSTEM_PROMPT (see agent.py's build_date_context_injector()):
    ContextInjector folds this text onto the trailing edge of the current
    call's latest user message — never into the system prompt, and never
    into durable conversation history — so it can't sit ahead of (or
    anywhere inside) a cached system-prompt prefix. Anthropic's cache
    requires an exact prefix match, so a date string glued to the front of
    the system prompt (this project's original approach, before prompt
    caching was added) would invalidate that cache once per UTC day at
    minimum — a real, documented cache-fragmentation failure mode ("Prevent
    Cache Fragmentation": move timestamps after the cache point).

    Args:
        today_iso: Today's date as an ISO 8601 date string (YYYY-MM-DD), in
            the traveler-relevant timezone the caller has chosen.
    """
    return (
        f"<now>Today's date is {today_iso}. Use this to resolve any "
        "relative dates the traveler mentions (e.g. \"next Friday\", \"in "
        "two weeks\") to concrete calendar dates, and to compute day-by-day "
        "dates for itinerary headings.</now>"
    )
