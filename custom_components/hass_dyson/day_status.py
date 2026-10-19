"""Daily "is the house clean today?" bookkeeping for the robot vacuum.

Pure logic, no Home Assistant imports, so it can be tested and replayed on real
data. The sensor in ``sensor.py`` (``DysonRobotDayStatusSensor``) feeds it the
cloud clean records and publishes the result.

Rules (agreed 8 okt 2026):

* A day runs from 04:00 to 04:00 local time.
* A room is *done today* when a clean ended it as ``CLEAN_COMPLETE`` on this
  day, in any method and whether the run was started by hand or by an
  automation. An interrupted room (``CLEAN_IN_PROGRESS``) or one that could
  not be cleaned (``CANT_CLEAN``) is not done and is tried again.
* Open rooms are ordered by the *day* they were last completed, oldest first
  (never completed counts as oldest), and ties are broken by a fixed list.
* A room that failed twice today (thin or ``CANT_CLEAN``) is given up for the
  rest of the day instead of being retried at every departure; the day change
  or a completed manual run clears it. Rooms the robot never reached
  (``CLEAN_PENDING``, e.g. someone came home early) are not attempts.
* What a room needs depends on its cleaning type (19 okt 2026): ``vacuum`` needs
  a completed vacuum pass, ``mop`` a completed mop pass, ``vacuumAndMop`` and
  ``vacuumThenMop`` both. A complete run counts for the parts its own type
  covers, so a vacuumed room that is switched to mopping becomes open again
  and one that is switched back to vacuum-only is done again. Water level,
  mop passes and strategy do not count (yet). A room whose wanted type is
  unknown is judged as before: complete today = done.
* ``CLEAN_COMPLETE`` is the robot's own verdict and it can be wrong: on 9 okt
  2026 it reported the Kinderkamer and the Slaapkamer complete after driving a
  few metres in through the door. A complete room therefore only counts when at
  least ``MIN_COVERAGE`` of its reachable floor was actually swept (measured
  spatially, so repeated passes do not inflate it). Observed on 18 room-runs:
  genuine runs 55-95 %, the three thin ones 6.5 %, 21.9 % and 28 %.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

DAY_START_HOUR = 4
COMPLETE = "CLEAN_COMPLETE"
NOT_REQUESTED = "CLEAN_NOT_REQUESTED"
PENDING = "CLEAN_PENDING"
CANT_CLEAN = "CANT_CLEAN"

# Fixed order for rooms whose last completion falls on the same day: short
# rooms first, the one next to the dock (Zithoek) before the big ones.
TIEBREAK: tuple[str, ...] = (
    "Keuken",
    "Gang",
    "Kinderkamer",
    "Wasruimte",
    "Slaapkamer",
    "Zithoek",
    "Woonkamer",
)

_MAX_PROCESSED = 60
STATE_VERSION = 2
MIN_COVERAGE = 0.40
# A room that failed (thin or could not be cleaned) this many times in one day
# is given up until the next day; a manual run that completes still counts.
MAX_FAILED_ATTEMPTS = 2
SWEEP_RADIUS_M = 0.15
GRID_M = 0.05
THIN = "TE_WEINIG_GEDEKT"
_LIVE_MAX_AGE = timedelta(hours=6)


VACUUM = "stofzuigen"
MOP = "dweilen"
_FULL = frozenset({VACUUM, MOP})


def components(clean_type: str | None) -> frozenset[str]:
    """The parts (vacuuming / mopping) a cleaning type covers.

    Unknown or missing types count as both, which keeps old data and unseen
    types behaving as before (a complete run = done).
    """
    if clean_type == "vacuum":
        return frozenset({VACUUM})
    if clean_type == "mop":
        return frozenset({MOP})
    return _FULL


def day_of(moment: datetime) -> date:
    """The 04:00-based day a (timezone-aware or naive local) moment belongs to."""
    return (moment - timedelta(hours=DAY_START_HOUR)).date()


def new_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "zones": {}, "processed": []}


def coverage_fraction(
    floor: list[tuple[float, float]],
    path: list[tuple[float, float]],
    radius: float = SWEEP_RADIUS_M,
    grid: float = GRID_M,
) -> float | None:
    """Share of the polygon ``floor`` lying within ``radius`` m of the path.

    ``None`` when it cannot be measured (too few points, no Pillow).
    """
    if len(floor) < 3:
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover
        return None
    xs = [p[0] for p in floor]
    ys = [p[1] for p in floor]
    x0, y0 = min(xs) - radius, min(ys) - radius
    width = int((max(xs) - x0 + radius) / grid) + 1
    height = int((max(ys) - y0 + radius) / grid) + 1
    area = Image.new("L", (width, height), 0)
    ImageDraw.Draw(area).polygon(
        [((x - x0) / grid, (y - y0) / grid) for x, y in floor], fill=255
    )
    swept = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(swept)
    r = radius / grid
    for x, y in path:
        px, py = (x - x0) / grid, (y - y0) / grid
        if -r <= px <= width + r and -r <= py <= height + r:
            draw.ellipse([px - r, py - r, px + r, py + r], fill=255)
    floor_cells = area.histogram()[255]
    if not floor_cells:
        return None
    both = Image.composite(area, Image.new("L", area.size, 0), swept)
    return both.histogram()[255] / floor_cells


def _zone(state: dict[str, Any], name: str) -> dict[str, Any]:
    entry = state["zones"].setdefault(
        name,
        {
            "last_completed": None,
            "last_vacuumed": None,
            "last_mopped": None,
            "live_completed": None,
            "last_coverage": None,
            "last_status": None,
            "last_attempt": None,
            "attempt_day": None,
            "attempts": 0,
        },
    )
    # A state saved before vacuuming and mopping were tracked apart counted a
    # complete room as both.
    entry.setdefault("last_vacuumed", entry.get("last_completed"))
    entry.setdefault("last_mopped", entry.get("last_completed"))
    return entry


def ingest_record(
    state: dict[str, Any],
    clean_id: str,
    when: datetime,
    zones: list[dict[str, Any]],
    coverage: dict[str, float | None] | None = None,
    run_components: dict[str, frozenset[str]] | None = None,
) -> bool:
    """Fold one finished clean into ``state``. Returns False if already seen.

    ``when`` is the moment the clean ended (its start if the end is unknown).
    Records must be fed oldest first so ``last_status`` ends up being the
    newest one. ``coverage`` maps room name to the measured swept share of its
    floor; a ``CLEAN_COMPLETE`` room below ``MIN_COVERAGE`` is recorded as
    ``THIN`` and does not count as completed. A complete room counts for the
    parts of its own cleaning type (``zone["settings"]["cleanType"]``),
    narrowed by ``run_components`` (what the wanted settings were when the run
    started) when the record's own settings may lag behind a recent change.
    """
    if clean_id in state["processed"]:
        return False
    stamp = when.isoformat()
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        name, status = zone.get("name"), zone.get("cleanStatus")
        if (
            not isinstance(name, str)
            or not name
            or status
            in (
                None,
                NOT_REQUESTED,
                PENDING,
            )
        ):
            continue
        entry = _zone(state, name)
        entry["live_completed"] = None  # the finished record is authoritative
        cov = (coverage or {}).get(name)
        thin = status == COMPLETE and cov is not None and cov < MIN_COVERAGE
        if thin:
            status = THIN
        day = day_of(when).isoformat()
        entry["attempts"] = entry["attempts"] + 1 if entry["attempt_day"] == day else 1
        entry["attempt_day"] = day
        if entry["last_attempt"] is None or stamp >= entry["last_attempt"]:
            entry["last_attempt"] = stamp
            entry["last_status"] = status
            entry["last_coverage"] = cov
        if status == COMPLETE:
            settings = zone.get("settings")
            record_type = (
                settings.get("cleanType") if isinstance(settings, dict) else None
            )
            done = components(record_type)
            if run_components and name in run_components:
                done = done & run_components[name]
            if VACUUM in done and (
                entry.get("last_vacuumed") is None or stamp > entry["last_vacuumed"]
            ):
                entry["last_vacuumed"] = stamp
            if MOP in done and (
                entry.get("last_mopped") is None or stamp > entry["last_mopped"]
            ):
                entry["last_mopped"] = stamp
        if status == COMPLETE and (
            entry["last_completed"] is None or stamp > entry["last_completed"]
        ):
            entry["last_completed"] = stamp
    state["processed"].append(clean_id)
    del state["processed"][:-_MAX_PROCESSED]
    return True


def mark_complete(state: dict[str, Any], name: str, when: datetime) -> bool:
    """Note a room that finished during the clean in progress (live signal).

    Kept apart from ``last_completed``: the finished record has the final say
    (including the coverage check) and clears this mark when it arrives.
    """
    entry = _zone(state, name)
    stamp = when.isoformat()
    if entry["live_completed"] is not None and entry["live_completed"] >= stamp:
        return False
    entry["live_completed"] = stamp
    return True


def _sort_key(name: str, entry: dict[str, Any] | None) -> tuple[Any, ...]:
    last = entry.get("last_completed") if entry else None
    last_day = (
        day_of(datetime.fromisoformat(last)).isoformat() if last else ""
    )  # "" sorts before any date: never completed = oldest
    tie = TIEBREAK.index(name) if name in TIEBREAK else len(TIEBREAK)
    return (last_day, tie, name)


def _stamp(entry: dict[str, Any], key: str) -> str | None:
    """Timestamp for a part; states from before the split fall back to
    ``last_completed`` (they counted as both parts)."""
    if key in entry:
        return entry[key]
    return entry.get("last_completed")


def evaluate(
    state: dict[str, Any],
    now: datetime,
    zone_names: list[str],
    wanted: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Today's status for ``zone_names`` (the rooms on the current map).

    ``wanted`` maps a room to its current cleaning type (``vacuum``, ``mop``,
    ``vacuumAndMop``, ``vacuumThenMop``); rooms missing from it are judged on
    "complete today" alone.
    """
    today = day_of(now).isoformat()
    zones = state["zones"]
    rooms: dict[str, dict[str, Any]] = {}
    for name in zone_names:
        entry = zones.get(name) or {}
        last = entry.get("last_completed")
        live = entry.get("live_completed")
        done_today: set[str] = set()
        for part, key in ((VACUUM, "last_vacuumed"), (MOP, "last_mopped")):
            stamp = _stamp(entry, key)
            if stamp and day_of(datetime.fromisoformat(stamp)).isoformat() == today:
                done_today.add(part)
        wanted_type = (wanted or {}).get(name)
        needs = components(wanted_type) if wanted_type else None
        if needs is None:
            done = (
                bool(last) and day_of(datetime.fromisoformat(last)).isoformat() == today
            )
        else:
            done = needs <= done_today
        if not done and live:
            live_at = datetime.fromisoformat(live)
            done = (
                day_of(live_at).isoformat() == today and now - live_at < _LIVE_MAX_AGE
            )
        attempts = entry.get("attempts", 0) if entry.get("attempt_day") == today else 0
        rooms[name] = {
            "klaar_vandaag": done,
            "laatst_voltooid": last,
            "laatste_status": entry.get("last_status"),
            "dekking": entry.get("last_coverage"),
            "pogingen_vandaag": attempts,
            "gewenst": wanted_type,
            "gedaan_vandaag": sorted(done_today),
            "ontbreekt": sorted(needs - done_today) if needs is not None else [],
        }
    for r in rooms.values():
        r["opgegeven"] = (
            not r["klaar_vandaag"]
            and r["pogingen_vandaag"] >= MAX_FAILED_ATTEMPTS
            and r["laatste_status"] in (THIN, CANT_CLEAN)
        )
    open_rooms = sorted(
        (n for n, r in rooms.items() if not r["klaar_vandaag"] and not r["opgegeven"]),
        key=lambda n: _sort_key(n, zones.get(n)),
    )
    given_up = [n for n in zone_names if rooms[n]["opgegeven"]]
    done_rooms = [n for n in zone_names if rooms[n]["klaar_vandaag"]]
    return {
        "dag": today,
        "kamers": rooms,
        "open": open_rooms,
        "klaar": done_rooms,
        "alles_klaar": bool(zone_names) and not open_rooms and not given_up,
        "opgegeven_vandaag": given_up,
        "onvolledig_vandaag": [
            n
            for n, r in rooms.items()
            if not r["klaar_vandaag"] and r["gedaan_vandaag"] and r["ontbreekt"]
        ],
        "cant_clean_vandaag": [
            n
            for n, r in rooms.items()
            if not r["klaar_vandaag"] and r["laatste_status"] == CANT_CLEAN
        ],
        "te_weinig_gedekt_vandaag": [
            n
            for n, r in rooms.items()
            if not r["klaar_vandaag"] and r["laatste_status"] == THIN
        ],
    }
