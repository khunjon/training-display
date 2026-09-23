#!/usr/bin/env python3
"""Render the Training Display frame: today.json + today.png (800x480, 1-bit).

One question, one screen: "what are we training today?" — each person's session
with a time and place, a countdown to the next race, and a quote for the day.
Days we train together are drawn as one outing rather than a row each.

Sources (all optional except the calendar)
  - a shared Google Calendar, read with a service account
  - a goals folder of Markdown notes with YAML frontmatter -> days to the next race
  - a per-person activity-log CSV                          -> DONE state
  - a Markdown quotes file, one bulleted quote per line    -> quote of the day

Writes only to the output dir (default ~/.local/state/training-display/).

Usage
  render.py                     # today, live calendar
  render.py --date 2026-09-21   # another day
  render.py --fixture f.json    # skip the calendar, use saved events
  render.py --dump-events       # also save today's raw events as a fixture
  render.py --config path.json  # default ~/.config/training-display/config.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_CONFIG = "~/.config/training-display/config.json"
W, H = 800, 480

RUN_WORDS = ("run", "threshold", "tempo", "intervals", "fartlek", "strides", "race")
STRENGTH_WORDS = ("strength", "gym", "lift")
REST_WORDS = ("rest", "mobility", "off")

TOGETHER_TAGS = {"together", "us", "both"}
APART_TAGS = {"apart", "solo", "separate"}
TOGETHER_WINDOW_MIN = 30  # two sessions starting this close, in the same place, are one outing

# The panel's LiPo reads ~4.2 V full and browns out near 3.3 V; below 3.6 V it is on the
# steep end of the curve with days, not weeks, left. Recovery needs +0.1 V (i.e. a charge),
# so a reading that wobbles at the line cannot flip the mark — and the panel — every hour.
BATTERY_LOW_V = 3.6
BATTERY_HYSTERESIS_V = 0.1

# Neither display face has a single symbol glyph — not even ♥ — so an emoji anyone types
# into the calendar comes out as a .notdef box. Noto Emoji is monochrome and draws them.
EMOJI_FONT = "NotoEmoji-Regular.ttf"
# Sequence glue we cannot lay out without libraqm: a variation selector, a skin tone, a
# keycap, or a ZWJ and what it joins would each land as its own glyph. Keep the first
# character of a sequence and drop the rest, so 🏋️‍♀️ draws as 🏋 rather than three pictures.
EMOJI_GLUE = re.compile("‍.|[︎️⃣\U0001F3FB-\U0001F3FF]", re.S)
# Noto draws the coloured hearts as hatching and stipple, standing in for a colour this
# screen does not have — at 24 px that is a smudge. The heart *suit* is a solid shape, so
# every heart becomes one and looks like a heart. Nothing else needs this: the outline
# star and the ticks hold up fine, and there is no solid star in the font to swap to.
EMOJI_SOLID = {c: "♥" for c in "❤♡❣\U0001F5A4\U0001F90D\U0001F90E"
               "\U0001F493\U0001F494\U0001F495\U0001F496\U0001F497\U0001F498\U0001F499"
               "\U0001F49A\U0001F49B\U0001F49C\U0001F49D\U0001F49F"}


# ---------------------------------------------------------------- config

def _p(v: str | None) -> Path | None:
    return Path(v).expanduser() if v else None


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    cfg = json.loads(Path(path).expanduser().read_text())
    cfg.setdefault("timezone", "UTC")
    cfg.setdefault("service_account_key", "~/.config/training-display/sa.json")
    cfg.setdefault("output_dir", "~/.local/state/training-display")
    cfg.setdefault("fonts_dir", "~/.local/state/training-display/fonts")
    people = cfg.get("people") or [{"name": "Me"}]
    for p in people:
        p.setdefault("emails", [])
        p["emails"] = [e.lower() for e in p["emails"]]
    cfg["people"] = people
    return cfg


# ---------------------------------------------------------------- calendar

def fetch_events(day: dt.date, cfg: dict) -> list[dict]:
    """Raw Google Calendar events overlapping `day` in the configured time zone."""
    import requests
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    tz = ZoneInfo(cfg["timezone"])
    creds = service_account.Credentials.from_service_account_file(
        str(_p(cfg["service_account_key"])), scopes=["https://www.googleapis.com/auth/calendar.readonly"]
    )
    creds.refresh(Request())
    start = dt.datetime.combine(day, dt.time(), tz)
    r = requests.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{cfg['calendar_id']}/events",
        headers={"Authorization": f"Bearer {creds.token}"},
        params={
            "timeMin": start.isoformat(),
            "timeMax": (start + dt.timedelta(days=1)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeZone": cfg["timezone"],
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("items", [])


# ---------------------------------------------------------------- resolver

def _words(words: tuple[str, ...]) -> re.Pattern:
    """Whole words, plus the endings a title actually uses: 'runs', 'running', 'rested'.
    A bare substring test read 'coffee' and 'office' as rest, 'brunch' as a run."""
    return re.compile(r"\b(?:" + "|".join(words) + r")(?:s|es|ing|ning|ed)?\b")


RUN_RE, STRENGTH_RE, REST_RE = _words(RUN_WORDS), _words(STRENGTH_WORDS), _words(REST_WORDS)


def classify(label: str) -> str:
    """'run' | 'strength' | 'rest' | 'other' from a session title."""
    s = label.lower()
    if REST_RE.search(s) and "recovery run" not in s:
        return "rest"
    if STRENGTH_RE.search(s):
        return "strength"
    if RUN_RE.search(s):
        return "run"
    return "other"


def _fmt_time(ev: dict, day: dt.date, tz: ZoneInfo) -> str:
    st, en = ev.get("start", {}), ev.get("end", {})
    if "date" in st:  # all-day
        return ""
    s = dt.datetime.fromisoformat(st["dateTime"]).astimezone(tz)
    e = dt.datetime.fromisoformat(en["dateTime"]).astimezone(tz) if "dateTime" in en else None
    out = s.strftime("%H:%M")
    if e and e.date() == day and e > s:
        out += "–" + e.strftime("%H:%M")
    return out


def _sort_key(ev: dict) -> str:
    st = ev.get("start", {})
    return st.get("dateTime") or (st.get("date", "") + "T00:00:00")


def _strip_tags(text: str) -> tuple[str, set[str]]:
    """Pull trailing `#tags` off a line: 'Long run #together' -> ('Long run', {'together'})."""
    tags = set()
    while (m := re.search(r"\s+#([\w-]+)$", text)):
        tags.add(m.group(1).lower())
        text = text[: m.start()]
    return text.strip(), tags


def resolve(events: list[dict], day: dt.date, people: list[dict], tz: ZoneInfo | str = "UTC") -> dict[str, dict | None]:
    """One row per person: {name: session | None}.

    Who: an event created by one of a person's emails is theirs; a `Name:` title prefix
    overrides; anything else belongs to the first person in `people`.
    An event titled for everyone — `Alex + Sam:`, `Us:`, `Both:`, `Together:`, or any
    title tagged `#together` — belongs to all of them and is flagged as a shared outing;
    `#apart` flags the opposite, for a day that only looks shared. Tags never show.
    If someone has several events, the earliest timed one wins (an all-day 'Rest'
    loses to a real session).
    """
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    names = [p["name"] for p in people]
    by_email = {e: p["name"] for p in people for e in p["emails"]}
    alt = "|".join(re.escape(n) for n in names)
    prefix = re.compile(r"^\s*(" + alt + r")\s*[:\-–·]\s*(.+)$", re.I)
    group = re.compile(
        r"^\s*(?:(?:" + alt + r")(?:\s*(?:\+|&|/|,|and)\s*(?:" + alt + r"))+|us|both|together)\s*[:\-–·]\s*(.+)$", re.I
    )
    parsed: list[tuple[str, dict]] = []

    for ev in sorted(events, key=_sort_key):
        if ev.get("status") == "cancelled":
            continue
        title, tags = _strip_tags((ev.get("summary") or "").strip())
        if not title:
            continue
        shared = None
        if (m := group.match(title)):
            title, shared = m.group(1).strip(), True
            who = by_email.get((ev.get("creator") or {}).get("email", "").lower(), names[0])
        elif (m := prefix.match(title)):
            title = m.group(2).strip()
            who = next(n for n in names if n.lower() == m.group(1).lower())
        else:
            who = by_email.get((ev.get("creator") or {}).get("email", "").lower(), names[0])
        if tags & TOGETHER_TAGS:
            shared = True
        elif tags & APART_TAGS:
            shared = False
        parsed.append((who, {
            "label": title.upper(),
            "kind": classify(title),
            "time": _fmt_time(ev, day, tz),
            "place": (ev.get("location") or "").strip(),
            "all_day": "date" in ev.get("start", {}),
            "together": shared,
        }))

    # a shared event stands in for anyone who did not write one of their own: one
    # `Us: Long run` covers us both, but if we each entered our own we each keep it
    spoke = {who for who, sess in parsed if sess["together"]}
    rows: dict[str, list[dict]] = {n: [] for n in names}
    for who, sess in parsed:
        rows[who].append(sess)
        if sess["together"]:
            for n in names:
                if n != who and n not in spoke:
                    rows[n].append(dict(sess))

    def pick(lst: list[dict]) -> dict | None:
        if not lst:
            return None
        timed = [r for r in lst if not r["all_day"]]
        return (timed or lst)[0]

    return {n: pick(rows[n]) for n in names}


def _start_min(sess: dict) -> int | None:
    m = re.match(r"(\d{1,2}):(\d{2})", sess.get("time") or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _norm_place(place: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (place or "").lower()).split())


def togetherness(sessions: list[dict | None], window_min: int = TOGETHER_WINDOW_MIN) -> dict | None:
    """The shared time and place when today is one outing rather than two, else None.

    Two ways to be together. Either every session says so — a `#together` tag or an
    `Alex + Sam:` title — or they are plainly the same trip out of the house: same
    place, starts within `window_min` of each other, whatever the work each person
    does when they get there. `#apart` on any of them settles it the other way, and
    `window_min` of 0 drops the inference and leaves only the explicit marker.

    `same` says both are doing the identical session, so the display can collapse
    the two rows into one label.
    """
    if len(sessions) < 2 or not all(sessions):
        return None
    if any(s["together"] is False for s in sessions):
        return None
    starts = [_start_min(s) for s in sessions]
    if not all(s["together"] for s in sessions):  # not marked: infer from place and time
        places = {_norm_place(s["place"]) for s in sessions}
        if not window_min or len(places) != 1 or not places.pop():
            return None
        if any(t is None for t in starts) or max(starts) - min(starts) > window_min:
            return None
    lead = sessions[starts.index(min(starts))] if all(t is not None for t in starts) else sessions[0]
    labels = {s["label"] for s in sessions}
    same = len(labels) == 1
    return {
        # one time for the outing: the shared one if it is shared, else when the first of us starts
        "time": lead["time"] if len({s["time"] for s in sessions}) == 1 else (lead["time"] or "").split("–")[0],
        "place": next((s["place"] for s in sessions if s["place"]), ""),
        "same": same,
        "label": next(iter(labels)) if same else "",
    }


def _log_rows(day: dt.date, log_path: Path | None):
    if not log_path or not log_path.exists():
        return
    with log_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("date") == day.isoformat():
                yield row


def _kind_of(row: dict) -> str | None:
    t = row.get("type", "").lower()
    if t == "run":
        return "run"
    if t in ("weight_training", "workout", "strength"):
        return "strength"
    return None


def done_state(day: dt.date, kind: str, log_path: Path | None) -> str | None:
    """'9.7 KM · 7:21 · HR 150' if the log has a matching session that day, else None."""
    if kind not in ("run", "strength"):
        return None
    for row in _log_rows(day, log_path):
        if _kind_of(row) != kind:
            continue
        if kind == "run":
            bits = []
            if row.get("distance_km"):
                bits.append(f"{float(row['distance_km']):.1f} KM")
            if row.get("pace_min_km"):
                bits.append(row["pace_min_km"])
            if row.get("avg_hr"):
                bits.append(f"HR {row['avg_hr']}")
            return " · ".join(bits) or "DONE"
        m = re.match(r"(\d+):(\d+):(\d+)", row.get("moving_time", ""))
        return f"{int(m.group(1)) * 60 + int(m.group(2))} MIN" if m else "DONE"
    return None


def logged_session(day: dt.date, log_path: Path | None) -> dict | None:
    """When nothing was planned but something was logged, the log wins: build a row from it."""
    for row in _log_rows(day, log_path):
        kind = _kind_of(row)
        if not kind:
            continue
        name = re.split(r"\s+[—–-]\s+", row.get("name") or "", 1)[0].strip() or kind
        return {"label": name.upper(), "kind": kind, "time": "", "place": row.get("location", "").strip(),
                "all_day": False, "together": None}
    return None


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def race_countdown(day: dt.date, goals_dir: Path | None) -> dict | None:
    """Nearest note with `status: active` and a full `target_date` on/after `day`."""
    if not goals_dir or not goals_dir.is_dir():
        return None
    best = None
    for p in sorted(goals_dir.glob("*.md")):
        fm = _parse_frontmatter(p)
        if fm.get("status") != "active":
            continue
        try:
            d = dt.date.fromisoformat(fm.get("target_date", ""))
        except ValueError:
            continue  # month-only targets like 2026-11 are not races
        if d < day:
            continue
        if best is None or d < best["date"]:
            best = {"name": p.stem, "days": (d - day).days, "date": d}
    if best:
        best["date"] = best["date"].isoformat()
    return best


def load_quotes(path: Path | None) -> list[tuple[str, str, set[str]]]:
    """Bulleted lines `- "text" — author #tag`; everything else in the file is ignored.

    Trailing `#tags` are stripped from the line and returned as the third element.
    """
    out = []
    if not path or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        body, tags = _strip_tags(line[2:].strip())
        m = re.match(r'^[“"](.+?)[”"]\s*[—–-]\s*(.+)$', body)
        text, author = (m.group(1).strip(), m.group(2).strip()) if m else (body.strip('"“”'), "")
        out.append((text, author, tags))
    return out


def quote_for(day: dt.date, quotes: list[tuple], kind: str | None = None) -> tuple[str, str] | None:
    """Rotate one quote per day: stable within a day, consecutive days differ.

    `kind` is the first person's session kind. On a rest day the rotation runs over
    quotes tagged #rest; on other days over untagged ones. Each pool rotates on its
    own, so a rest quote never spends a training-day slot. Missing pool -> all.
    """
    if not quotes:
        return None
    tagged = [q for q in quotes if "rest" in (q[2] if len(q) > 2 else ())]
    plain = [q for q in quotes if q not in tagged]
    pool = (tagged if kind == "rest" else plain) or quotes
    q = pool[day.toordinal() % len(pool)]
    return (q[0], q[1])


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def special_for(day: dt.date, special_days: list[dict] | None) -> tuple[str, str] | None:
    """A message that replaces the quote on a special date.

    Config entries: {"date": "MM-DD", "text": "...", "author": "...", "since": 2024}.
    In `text`, `{n}` is the years since `since` (e.g. "Happy {nth} anniversary" -> "2nd").
    `author` is optional and drawn like a quote's attribution.
    """
    for sd in special_days or []:
        if sd.get("date") != day.strftime("%m-%d"):
            continue
        text = sd.get("text", "")
        if sd.get("since"):
            n = day.year - int(sd["since"])
            text = text.replace("{nth}", _ordinal(n)).replace("{n}", str(n))
        return (text, sd.get("author", ""), sd.get("icon", ""))
    return None


def _draw_heart(d, x: int, y: int, size: int, fill=0):
    """A solid heart with its bounding box's top-left at (x, y): two lobes and a point."""
    r = size / 4
    d.ellipse([x, y, x + 2 * r, y + 2 * r], fill=fill)
    d.ellipse([x + 2 * r, y, x + 4 * r, y + 2 * r], fill=fill)
    d.polygon([(x, y + r), (x + 4 * r, y + r), (x + 2 * r, y + size)], fill=fill)


def _draw_cake(d, x: int, y: int, size: int, fill=0):
    """A two-tier cake with a lit candle, bounding box top-left at (x, y)."""
    s = size / 40  # designed on a 40x40 grid
    # three candles with teardrop flames
    for cx in (x + 11 * s, x + 20 * s, x + 29 * s):
        d.polygon([(cx, y), (cx + 3 * s, y + 6 * s), (cx, y + 9 * s), (cx - 3 * s, y + 6 * s)], fill=fill)
        d.rectangle([cx - 1.5 * s, y + 10 * s, cx + 1.5 * s, y + 19 * s], fill=fill)
    # top tier (outlined) and bottom tier (solid) with a plate line
    d.rounded_rectangle([x + 6 * s, y + 19 * s, x + 34 * s, y + 29 * s], radius=int(3 * s), outline=fill, width=max(2, int(2 * s)))
    d.rectangle([x + 2 * s, y + 29 * s, x + 38 * s, y + 38 * s], fill=fill)
    d.line([(x, y + 40 * s), (x + 40 * s, y + 40 * s)], fill=fill, width=max(2, int(2 * s)))


ICONS = {"heart": _draw_heart, "cake": _draw_cake}


def battery_low(device: dict | None, was_low: bool = False, low_v: float = BATTERY_LOW_V) -> bool:
    """Whether the panel's battery wants charging, from the headers `server.py` saved.

    A dead panel freezes on its last frame and looks current, so this is drawn a few
    days early. Once low it stays low until the voltage is `BATTERY_HYSTERESIS_V` above
    the line. No reading (no device yet, a half-written file) keeps the previous answer.
    """
    try:
        v = float((device or {})["Battery-Voltage"])
    except (KeyError, TypeError, ValueError):
        return was_low
    return v < low_v + (BATTERY_HYSTERESIS_V if was_low else 0)


def _draw_battery(d, x: int, y: int, w: int = 30, h: int = 15, fill=0):
    """An almost-empty battery, bounding box top-left at (x, y): outline, terminal nub, one sliver."""
    nub = 3
    d.rounded_rectangle([x, y, x + w - nub, y + h], radius=2, outline=fill, width=2)
    d.rectangle([x + w - nub, y + h // 2 - 3, x + w, y + h // 2 + 3], fill=fill)
    d.rectangle([x + 4, y + 4, x + 8, y + h - 4], fill=fill)


def build(day: dt.date, events: list[dict], cfg: dict, now: dt.datetime | None = None) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    sessions = resolve(events, day, cfg["people"], tz)
    special = special_for(day, cfg.get("special_days"))
    rows = []
    for p in cfg["people"]:
        log = _p(p.get("activity_log"))
        sess = sessions[p["name"]] or logged_session(day, log)
        rows.append({"name": p["name"], "session": sess, "done": done_state(day, sess["kind"], log) if sess else None})
    first = rows[0]["session"] if rows else None
    first_kind = first["kind"] if first else None
    together = togetherness([r["session"] for r in rows], int(cfg.get("together_window_min", TOGETHER_WINDOW_MIN) or 0))
    if together:
        # one label for everyone only if there is one thing to say about the doing of it too
        together["collapse"] = together["same"] and len({bool(r["done"]) for r in rows}) == 1
    return {
        "date": day.isoformat(),
        "date_label": day.strftime("%a %d %b").upper(),
        "race": race_countdown(day, _p(cfg.get("goals_dir"))),
        "rows": rows,
        "together": together,
        "quote": special[:2] if special else quote_for(day, load_quotes(_p(cfg.get("quotes_file"))), kind=first_kind),
        "special": special is not None,  # a message, not a quotation: drawn without quote marks
        "icon": special[2] if special else "",  # "heart" or "cake", drawn beside the message
        "updated": (now or dt.datetime.now(tz)).strftime("%H:%M"),
    }


# ---------------------------------------------------------------- render

def _font_loader(fonts_dir: Path):
    """Cached, because `fit` walks a dozen sizes and every face is asked for repeatedly —
    and because callers use the font object's identity to cache what it can draw."""
    from PIL import ImageFont

    loaded: dict[tuple[str, int], object] = {}

    def font(name: str, size: int):
        if (name, size) not in loaded:
            try:
                loaded[(name, size)] = ImageFont.truetype(str(fonts_dir / name), size)
            except OSError:  # sized, so layout and fitting still behave without the fonts
                loaded[(name, size)] = ImageFont.load_default(size)
        return loaded[(name, size)]

    return font


def _font_key(font) -> tuple:
    """Identify a font by what it is, not by `id()` — Pillow font objects are short-lived
    and a freed id gets handed to the next one, which would poison a glyph cache."""
    return (getattr(font, "path", ""), getattr(font, "size", 0))


def _has_glyph(font, ch: str, cache: dict) -> bool:
    """FreeType draws a missing character as .notdef, so compare it against one no font has."""
    fk = _font_key(font)
    if (fk, ch) not in cache:
        miss = cache.setdefault((fk, None), (font.getbbox(""), font.getlength("")))
        cache[(fk, ch)] = (font.getbbox(ch), font.getlength(ch)) != miss
    return cache[(fk, ch)]


def _runs(text: str, font, emoji, cache: dict) -> list[tuple[str, object, bool]]:
    """Split a string into (run, font, is_emoji) pieces, each drawn by a font that has it.

    Below U+2000 is ordinary text and never probed. Above it, the text face is asked
    first — it does own the dashes, the curly quotes and a tick — then the emoji face.
    A character neither one has is dropped: a gap reads better on a wall than a box.
    """
    out: list[list] = []
    for ch in EMOJI_GLUE.sub("", text):
        ch = EMOJI_SOLID.get(ch, ch)
        if ord(ch) < 0x2000 or _has_glyph(font, ch, cache):
            f, is_emoji = font, False
        elif emoji is not None and _has_glyph(emoji, ch, cache):
            f, is_emoji = emoji, True
        else:
            continue
        if out and out[-1][1] is f:
            out[-1][0] += ch
        else:
            out.append([ch, f, is_emoji])
    if out:  # a dropped emoji must not leave the line hanging off its margin
        out[0][0] = out[0][0].lstrip()
        out[-1][0] = out[-1][0].rstrip()
    return [(run, f, e) for run, f, e in out if run]


def _wrap(text: str, tlen, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if tlen(t, font) <= max_w or not cur:  # a word wider than the line gets a line of its own
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(data: dict, fonts_dir: str | Path = "~/.local/state/training-display/fonts"):
    from PIL import Image, ImageDraw

    fonts = Path(fonts_dir).expanduser()
    font = _font_loader(fonts)
    mono = lambda s: font("IBMPlexMono-Medium.ttf", s)  # noqa: E731
    mono_b = lambda s: font("IBMPlexMono-Bold.ttf", s)  # noqa: E731
    cond = lambda s: font("BarlowCondensed-Bold.ttf", s)  # noqa: E731
    cond_semi = lambda s: font("BarlowCondensed-SemiBold.ttf", s)  # noqa: E731
    # no emoji face installed -> `emoji_for` gives None and emoji are dropped, never boxed
    has_emoji = (fonts / EMOJI_FONT).is_file()

    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    M = 28

    glyphs: dict = {}

    def emoji_for(f):
        """An emoji face sized to sit with `f`: 0.8×, since these faces run tall next to caps."""
        return font(EMOJI_FONT, max(10, round(getattr(f, "size", 20) * 0.8))) if has_emoji else None

    def tlen(text, f) -> float:
        return sum(d.textlength(run, font=rf) for run, rf, _ in _runs(text, f, emoji_for(f), glyphs))

    def dtext(xy, text, f, fill=0):
        """Draw a string that may mix text and emoji, one run per font."""
        x0, y0 = xy
        drop = round(getattr(f, "size", 20) * 0.1)  # emoji sit high against cap height; nudge them down
        for run, rf, is_emoji in _runs(text, f, emoji_for(f), glyphs):
            d.text((x0, y0 + drop if is_emoji else y0), run, font=rf, fill=fill)
            x0 += d.textlength(run, font=rf)

    def clip(text, f, max_w, tail="…", force=False):
        """`text` if it fits, else cut short with `tail` so it stops at `max_w`."""
        if not force and tlen(text, f) <= max_w:
            return text
        full = text
        while text and tlen(text.rstrip() + tail, f) > max_w:
            text = text[:-1]
        if full[len(text):len(text) + 1] not in ("", " ") and " " in text:  # cut mid-word: back to the last whole one
            text = text.rsplit(" ", 1)[0]
        return text.rstrip(" ,;:·—–-") + tail

    def fit(text, mk, max_w, start, floor):
        """Shrink from `start` toward `floor` until it fits; past the floor, cut it short.
        Returns (text, font, size) — the text may have lost its end to an ellipsis."""
        size = start
        while size > floor and tlen(text, mk(size)) > max_w:
            size -= 2
        f = mk(size)
        return clip(text, f, max_w), f, size

    # --- top strip
    y = M
    dtext((M, y), data["date_label"], mono(26), 0)
    if data.get("race"):
        r = data["race"]
        txt, f, size = fit(f"{r['days']} DAYS TO {r['name'].upper()}", mono, W - 2 * M - 220, 26, 18)
        dtext((W - M - tlen(txt, f), y + (26 - size) // 2), txt, f, 0)
    y += 44
    d.line([(M, y), (W - M, y)], fill=0, width=3)

    # --- people rows (designed for two; more just get less room)
    rows = data.get("rows") or []
    n = max(len(rows), 1)
    area_top, area_h = y + 26, 244
    x = M + 110

    def drop_to_baseline(full, f) -> int:
        """How far to lower a label shrunk from face `full` to `f` so both sit on one baseline."""
        return full.getmetrics()[0] - f.getmetrics()[0]

    def done_badge(text: str, top: int):
        bw, bh = 150, 44
        bx = W - M - bw
        d.rounded_rectangle([bx, top, bx + bw, top + bh], radius=8, fill=0)
        _, f2, _ = fit("DONE", mono_b, bw - 16, 24, 16)
        dtext((bx + (bw - tlen("DONE", f2)) / 2, top + 8), "DONE", f2, 255)
        text, f3, _ = fit(text, mono, 220, 20, 14)
        dtext((W - M - tlen(text, f3), top + bh + 8), text, f3, 0)

    tg = data.get("together") if len(rows) > 1 else None
    if tg:
        # one band for the outing we share, then what each of us does once we are there
        fp = mono_b(20)
        pw = tlen("TOGETHER", fp) + 26
        d.rounded_rectangle([M, area_top, M + pw, area_top + 34], radius=8, fill=0)
        dtext((M + 13, area_top + 7), "TOGETHER", fp, 255)
        meta = "  ·  ".join(p for p in (tg.get("time"), tg.get("place")) if p)
        if meta:
            meta, f, _ = fit(meta, mono, W - 2 * M - pw - 20, 24, 16)
            dtext((M + pw + 20, area_top + 8), meta, f, 0)

    if tg and tg.get("collapse"):  # both doing the same thing: one label, both names under it
        label, f, size = fit(tg["label"], cond, W - 2 * M, 80, 32)
        dtext((max(M, (W - tlen(label, f)) / 2), area_top + 52), label, f, 0)
        names = "  ·  ".join(row["name"].upper() for row in rows)
        fn = mono(22)
        dtext(((W - tlen(names, fn)) / 2, area_top + 62 + int(size * 1.1)), names, fn, 0)
        done = next((row["done"] for row in rows if row.get("done")), None)
        if done:
            t, fd, _ = fit(f"DONE  ·  {done}", mono_b, W - 2 * M, 20, 14)
            dtext(((W - tlen(t, fd)) / 2, area_top + 98 + int(size * 1.1)), t, fd, 0)
    elif tg:  # together, but each with our own work: no divider, and the time and place said once
        line_h = (area_h - 52) // n
        for i, row in enumerate(rows):
            y0 = area_top + 52 + i * line_h
            dtext((M, y0 + 12), row["name"].upper(), mono(20), 0)
            sess, done = row.get("session"), row.get("done")
            if not sess:
                continue
            label, f, _ = fit(sess["label"], cond, W - M - (170 if done else 0) - x, 54, 32)
            dtext((x, y0 + drop_to_baseline(cond(54), f)), label, f, 0)
            if done:
                done_badge(done, y0 + 4)
    else:
        row_h = area_h // n
        for i, row in enumerate(rows):
            y0 = area_top + i * row_h
            dtext((M, y0 + 6), row["name"].upper(), mono(20), 0)
            sess, done = row.get("session"), row.get("done")
            if not sess:
                if i == 0:  # offline: say so, or an unreachable calendar reads as an empty day
                    dtext((x, y0 - 4), "CALENDAR OFFLINE" if data.get("offline") else "NO PLAN YET", cond_semi(48), 0)
                else:
                    d.line([(x, y0 + 22), (x + 44, y0 + 22)], fill=0, width=5)
            else:
                right_limit = W - M - (170 if done else 0)
                label, f, _ = fit(sess["label"], cond, right_limit - x, 64, 36)
                dtext((x, y0 - 8 + drop_to_baseline(cond(64), f)), label, f, 0)
                parts = [p for p in (sess["time"] or ("ALL DAY" if sess["all_day"] else ""), sess["place"]) if p]
                if parts:
                    meta = "  ·  ".join(parts)
                    # stop short of the done column: a long place name used to run into it
                    meta, fm, _ = fit(meta, mono, W - M - x - (236 if done else 0), 24, 14)
                    dtext((x, y0 + 64), meta, fm, 0)
                if done:
                    done_badge(done, y0 + 4)
            if i < len(rows) - 1:
                d.line([(x, y0 + row_h - 24), (W - M, y0 + row_h - 24)], fill=0, width=1)

    # --- quote
    qy = y + 270
    d.line([(M, qy), (W - M, qy)], fill=0, width=3)
    q = data.get("quote")
    if q:
        text, author = q
        qx = M
        if data.get("icon") in ICONS:
            ICONS[data["icon"]](d, M, qy + 18, 44)
            qx = M + 62
        size = 34
        while True:
            f = cond_semi(size)
            lines = _wrap(text if data.get("special") else f"“{text}”", tlen, f, W - M - qx)
            if len(lines) <= 2 or size <= 22:
                break
            size -= 2
        if len(lines) > 2:  # still too long at the floor: end the second line on an ellipsis
            lines = [lines[0], clip(lines[1], f, W - M - qx, tail="…" if data.get("special") else "…”", force=True)]
        ly = qy + 18
        for ln in lines:
            dtext((qx, ly), ln, f, 0)
            ly += int(size * 1.15)
        if author:
            dtext((qx, ly + 4), f"— {author}", mono(18), 0)

    # --- footer: only when stale. A fresh frame carries no clock, so re-rendering the same
    # day gives the same bytes and the panel (which redraws on a hash change) stays still.
    # The battery mark is a flag, never the voltage, for the same reason.
    fx = W - M  # the footer fills from the right edge
    if data.get("battery_low"):
        _draw_battery(d, fx - 30, H - M + 6)
        fx -= 42
    if data.get("stale"):
        note = f"stale · {data['stale']}"
        f = mono(14)
        dtext((fx - tlen(note, f), H - M + 6), note, f, 0)

    return img.convert("1", dither=Image.NONE)


# ---------------------------------------------------------------- main

def stale_frame(day: dt.date, cfg: dict, last: dict | None) -> dict:
    """What to draw when the calendar is unreachable — never a blank wall, never yesterday.

    The last good frame if it is today's. Otherwise today built from everything local
    (date, countdown, quote, logged sessions), with the empty rows saying the calendar
    is offline rather than that nothing is planned.
    """
    if last and last.get("date") == day.isoformat():
        data, since = dict(last), last.get("updated", "")
    else:
        data = build(day, [], cfg)
        data["offline"] = True
        since = f"{last.get('date_label', '')} {last.get('updated', '')}" if last else ""
    since = since.split(" (")[0].strip()  # frames from before `stale` existed carried "(stale)" in `updated`
    data["stale"] = f"last good {since}" if since else "calendar not reached yet"
    return data


def _write_atomic(path: Path, write) -> None:
    """Write via a temp file and a rename, so the server never serves (or hashes) half a file."""
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_frame(img, path: Path) -> None:
    _write_atomic(path, lambda t: img.save(t, format="PNG"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in the configured time zone)")
    ap.add_argument("--fixture", help="JSON file of raw calendar events; skips the calendar")
    ap.add_argument("--dump-events", action="store_true", help="save the raw events to <output_dir>/events-<date>.json")
    ap.add_argument("--out", help="output dir (default: config output_dir)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["timezone"])
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz).date()
    out = _p(args.out) or _p(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    last = _read_json(out / "today.json")
    low = battery_low(_read_json(out / "device.json"), bool(last and last.get("battery_low")),
                      float(cfg.get("battery_low_v", BATTERY_LOW_V)))

    if args.fixture:
        events = json.loads(Path(args.fixture).read_text())
    else:
        try:
            events = fetch_events(day, cfg)
        except Exception as e:  # keep the wall current and honest; never a blank
            sys.stderr.write(f"calendar fetch failed: {e}\n")
            data = stale_frame(day, cfg, last)  # today.json stays the last *good* data
            data["battery_low"] = low
            save_frame(render(data, cfg["fonts_dir"]), out / "today.png")
            sys.stderr.write(f"drew a stale frame ({'offline rows' if data.get('offline') else 'last good'})\n")
            return 1
        if args.dump_events:
            (out / f"events-{day.isoformat()}.json").write_text(json.dumps(events, indent=1))

    data = build(day, events, cfg)
    data["battery_low"] = low
    _write_atomic(out / "today.json", lambda t: t.write_text(json.dumps(data, indent=1, ensure_ascii=False)))
    save_frame(render(data, cfg["fonts_dir"]), out / "today.png")
    summary = "  ".join(f"{r['name']}={r['session'] and r['session']['label']}{' DONE' if r['done'] else ''}" for r in data["rows"])
    if data.get("together"):
        summary = f"[together] {summary}"
    if low:
        summary += "  [battery low]"
    print(f"{out / 'today.png'}  {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
