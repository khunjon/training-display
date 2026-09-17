#!/usr/bin/env python3
"""Render the Training Display frame: today.json + today.png (800x480, 1-bit).

One question, one screen: "what are we training today?" — each person's session
with a time and place, a countdown to the next race, and a quote for the day.

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

def classify(label: str) -> str:
    """'run' | 'strength' | 'rest' | 'other' from a session title."""
    s = label.lower()
    if any(w in s for w in REST_WORDS) and "recovery run" not in s:
        return "rest"
    if any(w in s for w in STRENGTH_WORDS):
        return "strength"
    if any(w in s for w in RUN_WORDS):
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


def resolve(events: list[dict], day: dt.date, people: list[dict], tz: ZoneInfo | str = "UTC") -> dict[str, dict | None]:
    """One row per person: {name: session | None}.

    Who: an event created by one of a person's emails is theirs; a `Name:` title prefix
    overrides; anything else belongs to the first person in `people`.
    If someone has several events, the earliest timed one wins (an all-day 'Rest'
    loses to a real session).
    """
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    names = [p["name"] for p in people]
    by_email = {e: p["name"] for p in people for e in p["emails"]}
    prefix = re.compile(r"^\s*(" + "|".join(re.escape(n) for n in names) + r")\s*[:\-–·]\s*(.+)$", re.I)
    rows: dict[str, list[dict]] = {n: [] for n in names}

    for ev in sorted(events, key=_sort_key):
        if ev.get("status") == "cancelled":
            continue
        title = (ev.get("summary") or "").strip()
        if not title:
            continue
        who = names[0]
        m = prefix.match(title)
        if m:
            who = next(n for n in names if n.lower() == m.group(1).lower())
            title = m.group(2).strip()
        else:
            who = by_email.get((ev.get("creator") or {}).get("email", "").lower(), who)
        rows[who].append(
            {
                "label": title.upper(),
                "kind": classify(title),
                "time": _fmt_time(ev, day, tz),
                "place": (ev.get("location") or "").strip(),
                "all_day": "date" in ev.get("start", {}),
            }
        )

    def pick(lst: list[dict]) -> dict | None:
        if not lst:
            return None
        timed = [r for r in lst if not r["all_day"]]
        return (timed or lst)[0]

    return {n: pick(rows[n]) for n in names}


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
        return {"label": name.upper(), "kind": kind, "time": "", "place": row.get("location", "").strip(), "all_day": False}
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
        body = line[2:].strip()
        tags = set()
        while (m := re.search(r"\s+#([\w-]+)$", body)):
            tags.add(m.group(1).lower())
            body = body[: m.start()]
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
    return {
        "date": day.isoformat(),
        "date_label": day.strftime("%a %d %b").upper(),
        "race": race_countdown(day, _p(cfg.get("goals_dir"))),
        "rows": rows,
        "quote": special[:2] if special else quote_for(day, load_quotes(_p(cfg.get("quotes_file"))), kind=first_kind),
        "special": special is not None,  # a message, not a quotation: drawn without quote marks
        "icon": special[2] if special else "",  # "heart" or "cake", drawn beside the message
        "updated": (now or dt.datetime.now(tz)).strftime("%H:%M"),
    }


# ---------------------------------------------------------------- render

def _font_loader(fonts_dir: Path):
    from PIL import ImageFont

    def font(name: str, size: int):
        try:
            return ImageFont.truetype(str(fonts_dir / name), size)
        except OSError:
            return ImageFont.load_default()

    return font


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(data: dict, fonts_dir: str | Path = "~/.local/state/training-display/fonts"):
    from PIL import Image, ImageDraw

    font = _font_loader(Path(fonts_dir).expanduser())
    mono = lambda s: font("IBMPlexMono-Medium.ttf", s)  # noqa: E731
    mono_b = lambda s: font("IBMPlexMono-Bold.ttf", s)  # noqa: E731
    cond = lambda s: font("BarlowCondensed-Bold.ttf", s)  # noqa: E731
    cond_semi = lambda s: font("BarlowCondensed-SemiBold.ttf", s)  # noqa: E731

    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    M = 28

    def fit(text, mk, max_w, start, floor):
        size = start
        while size > floor and d.textlength(text, font=mk(size)) > max_w:
            size -= 2
        return mk(size), size

    # --- top strip
    y = M
    d.text((M, y), data["date_label"], font=mono(26), fill=0)
    if data.get("race"):
        r = data["race"]
        txt = f"{r['days']} DAYS TO {r['name'].upper()}"
        f, size = fit(txt, mono, W - 2 * M - 220, 26, 18)
        d.text((W - M - d.textlength(txt, font=f), y + (26 - size) // 2), txt, font=f, fill=0)
    y += 44
    d.line([(M, y), (W - M, y)], fill=0, width=3)

    # --- people rows (designed for two; more just get less room)
    rows = data.get("rows") or []
    n = max(len(rows), 1)
    area_top, area_h = y + 26, 244
    row_h = area_h // n
    x = M + 110
    for i, row in enumerate(rows):
        y0 = area_top + i * row_h
        d.text((M, y0 + 6), row["name"].upper(), font=mono(20), fill=0)
        sess, done = row.get("session"), row.get("done")
        if not sess:
            if i == 0:
                d.text((x, y0 - 4), "NO PLAN YET", font=cond_semi(48), fill=0)
            else:
                d.line([(x, y0 + 22), (x + 44, y0 + 22)], fill=0, width=5)
        else:
            right_limit = W - M - (170 if done else 0)
            f, _ = fit(sess["label"], cond, right_limit - x, 64, 36)
            d.text((x, y0 - 8), sess["label"], font=f, fill=0)
            parts = [p for p in (sess["time"] or ("ALL DAY" if sess["all_day"] else ""), sess["place"]) if p]
            if parts:
                d.text((x, y0 + 64), "  ·  ".join(parts), font=mono(24), fill=0)
            if done:
                bw, bh = 150, 44
                bx, by = W - M - bw, y0 + 4
                d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=8, fill=0)
                f2, _ = fit("DONE", mono_b, bw - 16, 24, 16)
                d.text((bx + (bw - d.textlength("DONE", font=f2)) / 2, by + 8), "DONE", font=f2, fill=255)
                f3, _ = fit(done, mono, 220, 20, 14)
                d.text((W - M - d.textlength(done, font=f3), by + bh + 8), done, font=f3, fill=0)
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
            lines = _wrap(d, text if data.get("special") else f"“{text}”", f, W - M - qx)
            if len(lines) <= 2 or size <= 22:
                break
            size -= 2
        ly = qy + 18
        for ln in lines[:2]:
            d.text((qx, ly), ln, font=f, fill=0)
            ly += int(size * 1.15)
        if author:
            d.text((qx, ly + 4), f"— {author}", font=mono(18), fill=0)

    # --- footer
    upd = f"updated {data['updated']}"
    f = mono(14)
    d.text((W - M - d.textlength(upd, font=f), H - M + 6), upd, font=f, fill=0)

    return img.convert("1", dither=Image.NONE)


# ---------------------------------------------------------------- main

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

    if args.fixture:
        events = json.loads(Path(args.fixture).read_text())
    else:
        try:
            events = fetch_events(day, cfg)
        except Exception as e:  # keep the last good frame on the wall; never a blank
            sys.stderr.write(f"calendar fetch failed: {e}\n")
            last = out / "today.json"
            if last.exists():
                data = json.loads(last.read_text())
                data["updated"] = data.get("updated", "").split(" ")[0] + " (stale)"
                render(data, cfg["fonts_dir"]).save(out / "today.png")
                sys.stderr.write("re-rendered the last good frame with a stale stamp\n")
            return 1
        if args.dump_events:
            (out / f"events-{day.isoformat()}.json").write_text(json.dumps(events, indent=1))

    data = build(day, events, cfg)
    (out / "today.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    render(data, cfg["fonts_dir"]).save(out / "today.png")
    summary = "  ".join(f"{r['name']}={r['session'] and r['session']['label']}{' DONE' if r['done'] else ''}" for r in data["rows"])
    print(f"{out / 'today.png'}  {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
