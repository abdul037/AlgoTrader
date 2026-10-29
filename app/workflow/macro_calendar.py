"""US macro-event dates for the event-risk shadow check (operator 2026-10-18 -- SHADOW ONLY).

A static, dated table in code: nothing here touches the network or the database, so the
proposal path reads it as freely as a constant. ``event_risk.assess_macro`` is the rule.

Kinds (all dates are New York days):

* ``cpi`` -- the Consumer Price Index, 08:30 (BLS);
* ``ppi`` -- the Producer Price Index, 08:30 (BLS);
* ``jobs`` -- the Employment Situation report, 08:30 (BLS);
* ``fomc`` -- the FOMC rate decision: the statement day, the second day of each scheduled
  two-day meeting (the Federal Reserve's calendar gives no time of day).

Every date below was read off its official source page on ``CHECKED_ON`` (the BLS pages via a
web reader, as bls.gov refuses scripted downloads; the Federal Reserve page from its HTML),
and each BLS date was found on two pages: its release's schedule and the BLS year schedule
(the October 2026 dates, no longer on the release pages, on the 2026 year schedule alone, read
twice). Only those dates are listed. Each kind is vouched for from ``known_from`` through
``known_through`` only: a day outside that span is unknown, and an entry window that reaches
one reads ``calendar_unknown``, never clear. Not covered: an unscheduled FOMC meeting, a meeting
the Fed moves (its calendar: "each meeting date is tentative until confirmed at the meeting
immediately preceding it") and a release BLS moves (as the 2025 shutdown did). Update the table
and ``CHECKED_ON`` from the sources when one changes, and extend it (and ``known_through``)
when BLS publishes its 2028 schedules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

KINDS = ("cpi", "ppi", "jobs", "fomc")  # a window is clear only when all four vouch for it
CHECKED_ON = "2026-10-19"  # the day every date below was checked on its source page
TABLE_FROM = "2026-10-01"  # the first day the table vouches for (every kind)
BLS_2026 = "https://www.bls.gov/schedule/2026/home.htm"  # "Schedule of Selected Releases 2026"
BLS_2027 = "https://www.bls.gov/schedule/2027/home.htm"  # "Schedule of Selected Releases 2027"


@dataclass(frozen=True)
class Series:
    """One kind of release: its verified dates and the span the table vouches for."""

    kind: str
    name: str
    time_et: str | None  # as the source lists it (None: the source gives no time)
    sources: tuple[str, ...]
    known_from: str
    known_through: str
    dates: tuple[str, ...]


CALENDAR: tuple[Series, ...] = (
    Series(
        kind="cpi",
        name="Consumer Price Index (BLS)",
        time_et="08:30",
        sources=("https://www.bls.gov/schedule/news_release/cpi.htm", BLS_2026, BLS_2027),
        known_from=TABLE_FROM,
        known_through="2027-12-31",  # the 2027 schedule lists every 2027 release
        dates=(
            "2026-10-14", "2026-11-10", "2026-12-10",
            "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12", "2027-06-10",
            "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10",
        ),
    ),
    Series(
        kind="ppi",
        name="Producer Price Index (BLS)",
        time_et="08:30",
        sources=("https://www.bls.gov/schedule/news_release/ppi.htm", BLS_2026, BLS_2027),
        known_from=TABLE_FROM,
        known_through="2027-12-31",
        dates=(
            "2026-10-15", "2026-11-13", "2026-12-15",
            "2027-01-14", "2027-02-12", "2027-03-12", "2027-04-14", "2027-05-13", "2027-06-11",
            "2027-07-15", "2027-08-10", "2027-09-10", "2027-10-15", "2027-11-12", "2027-12-15",
        ),
    ),
    Series(
        kind="jobs",
        name="Employment Situation (BLS)",
        time_et="08:30",
        sources=("https://www.bls.gov/schedule/news_release/empsit.htm", BLS_2026, BLS_2027),
        known_from=TABLE_FROM,
        known_through="2027-12-31",
        dates=(
            "2026-10-02", "2026-11-06", "2026-12-04",
            "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07", "2027-06-04",
            "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03",
        ),
    ),
    Series(
        kind="fomc",
        name="FOMC statement (Federal Reserve)",
        time_et=None,
        # "Last Update: October 07, 2026"; it also notes "A two-day meeting is scheduled for
        # January 25-26, 2028", so no other scheduled meeting falls before that one.
        sources=("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",),
        known_from=TABLE_FROM,
        known_through="2028-01-26",
        dates=(
            "2026-10-28", "2026-12-09",
            "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
            "2027-10-27", "2027-12-08", "2028-01-26",
        ),
    ),
)


def scheduled(
    first: date, last: date, calendar: Any = None
) -> tuple[list[dict[str, str]], list[str]]:
    """The table's events from ``first`` through ``last`` (both included), by date, and the
    kinds the table cannot vouch for on every one of those days (a kind missing from the
    table, too: an empty table vouches for nothing). Pure, never I/O."""

    low, high = first.isoformat(), last.isoformat()
    events: list[dict[str, str]] = []
    covered: set[str] = set()
    for series in CALENDAR if calendar is None else calendar:
        if series.known_from <= low and high <= series.known_through:
            covered.add(series.kind)
        events += [{"date": d, "kind": series.kind} for d in series.dates if low <= d <= high]
    unknown = [kind for kind in KINDS if kind not in covered]
    return sorted(events, key=lambda e: (e["date"], e["kind"])), unknown


def describe(calendar: Any = None) -> dict[str, Any]:
    """The table's provenance and horizon, for the scorecard."""

    rows = CALENDAR if calendar is None else calendar
    return {
        "checked_on": CHECKED_ON,
        "kinds": {
            s.kind: {
                "name": s.name,
                "time_et": s.time_et,
                "known_from": s.known_from,
                "known_through": s.known_through,
                "dates": len(s.dates),
                "source": s.sources[0],
            }
            for s in rows
        },
        "known_through": min((s.known_through for s in rows), default=None),
    }
