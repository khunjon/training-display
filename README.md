# Training Display

A living-room e-paper screen that answers one question without anyone opening a laptop:
**"What are we training today?"**

Each person's session for the day with a time and place, a countdown to the next race,
and a quote to set the day's intention. Nothing else. It exists because my partner kept
asking "are you running later?" and the answer decides when we eat dinner.

```
THU 17 SEP                      73 DAYS TO AMAZING THAILAND 10K
───────────────────────────────────────────────────────────────
JON     THRESHOLD 3 × 10 MIN        18:00–19:30  TDP     [DONE]
BENYA   YOGA                        19:00–20:00  The Commons
───────────────────────────────────────────────────────────────
"How we spend our days is, of course, how we spend our lives."
 — Annie Dillard
```

## How it works

**A dumb screen, a smart always-on computer, a shared calendar.**

```
plan-writing tools ──▶  shared Google Calendar  ◀── partner (phone)
                                │  service account, read
                                ▼
goals/*.md (frontmatter) ─┐
activity_log.csv          ├─▶  render.py  ─▶  today.json + today.png (800×480, 1-bit)
quotes.md                 ┘        │            (launchd/cron: every 15 min)
                                   ▼
                          server.py  (LAN only)  /today.png  /api/setup  /api/display
                                   │  Wi-Fi, hourly fetch
                                   ▼
                          e-paper device on the wall  (TRMNL-firmware ESP32 + 7.5" panel)
```

- **All layout happens on the computer, in Python (Pillow).** The device downloads a finished
  bitmap and sleeps. That is why the hardware can be cheap: no fonts, no parsing, no logic on the ESP32.
- **The calendar is the session source.** A calendar event title *is* the short label, it carries a
  time (the thing the other person actually needs), and anyone can write to it from a phone.
- **Who is who:** an event created by one of a person's emails is theirs; a `Name:` title prefix
  overrides; everything else belongs to the first person in the config.
- **Some days are one outing, not two rows.** When you're going together the screen says so once,
  above whatever each of you is actually doing when you get there.
- **Done state comes from a local activity log**, never from writing back to the calendar. If
  something was logged that wasn't planned, the log wins and it still shows as done.
- **LAN only.** Nothing leaves the house; health data stays local.
- If the calendar is unreachable, the last good frame is re-rendered with a `stale` stamp — never a blank wall.
  On a new day it never shows yesterday's plan: the date, countdown and quote are today's, and the
  empty rows say `CALENDAR OFFLINE`.
- **A low panel battery shows as a small battery mark** in the corner, from the voltage the device
  reports on each check-in (`battery_low_v`, default 3.6 V). A dead e-paper panel freezes on its last
  frame and looks current, so the mark comes a few days early and stays until the panel is charged.
- **A fresh frame carries no clock**, so re-rendering an unchanged day produces the same bytes and the
  panel, which redraws only when the image hash changes, stays still. The footer appears only when stale.

## Hardware

- [Seeed × TRMNL 7.5" DIY kit](https://www.seeedstudio.com/TRMNL-7-5-Inch-OG-DIY-Kit-p-6481.html):
  800×480 e-ink panel + XIAO ESP32-S3 + battery. ~$45.
- The open-source [TRMNL firmware](https://github.com/usetrmnl/firmware) speaks a two-endpoint
  protocol (`/api/setup`, `/api/display`) that `server.py` implements — no cloud account needed.
- A deep picture frame from a stationery shop.

Any device that can fetch a PNG over Wi-Fi works; the phone page at `/` is the fallback.

## Setup

1. **Calendar.** Create a Google Calendar (e.g. "Training"), share it with everyone who should add
   sessions. In Google Cloud, create a project, enable the Calendar API, create a service account,
   download its JSON key to `~/.config/training-display/sa.json`, and share the calendar with the
   service account's email (read access is enough for the renderer).
2. **Config.** Copy `config.example.json` to `~/.config/training-display/config.json` and edit.
   Everything except `calendar_id` and `people` is optional — leave out `goals_dir`, `activity_log`
   or `quotes_file` and that zone is simply not drawn.
3. **Fonts.** `scripts/fetch_fonts.sh` downloads Barlow Condensed, IBM Plex Mono and Noto Emoji
   (all OFL) into `fonts_dir`. Noto Emoji is optional — see *Emoji* below.
4. **Python.** `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
5. **Render.** `venv/bin/python render.py` → `today.png`. Try `--date 2026-09-21`, or
   `--fixture fixtures/sample-events.json` to render without a calendar.
6. **Serve.** `venv/bin/python server.py` binds `0.0.0.0:8787` (LAN only — put it behind nothing
   that forwards from the internet). Routes: `/` (phone page), `/today.png`, `/today.json`, `/health`,
   and the TRMNL protocol `/api/setup`, `/api/display`, `/api/log`. An optional `"server"` block in
   the config sets `bind`, `port`, `refresh_rate` (device sleep, seconds), `quiet` (`{"from": "23:00",
   "until": "06:10"}`: the device sleeps through the window in one go; `null` disables) and `image_url`
   (only if the device cannot reuse the host it reached you on, e.g. behind a proxy).
7. **Schedule (macOS).** `scripts/install_launchd.sh` installs two launchd agents: the renderer at
   every 15 minutes from 06:00 to 22:45 (and on load), and the server kept alive. `--guard path/to/wrapper.sh`
   wraps the render job in your own notify-on-failure script; `--uninstall` removes both.
   On Linux, a cron line for `render.py` and a systemd unit for `server.py` do the same.
8. **Device.** Flash the [TRMNL firmware](https://github.com/usetrmnl/firmware), and in its captive
   portal set the server to `http://<your-computer-ip>:8787`. The device calls `/api/setup` once,
   then `/api/display` every `refresh_rate` seconds; it only redraws when the `filename` (a hash of
   the PNG) changes. Use a fixed IP or DHCP reservation rather than `.local` — mDNS on the ESP32 is
   not reliable on every router.

## Session vocabulary

Titles are free text, but the renderer classifies them so it knows which log rows count as "done":
words like *run, threshold, tempo, intervals, long run* → run; *strength, gym* → strength;
*rest, mobility* → rest; anything else (yoga, pilates, pickleball) → other.

## Together days

Sunday's long run is one trip to the park, not two independent sessions, so the screen draws it as
one. Two ways to say so:

- **Mark it.** Tag any title `#together` (the tag never shows), or title one event for everyone:
  `Alex + Sam:`, `Alex and Sam:`, `Us:`, `Both:`, `Together:`. A single marked event covers everyone
  who didn't enter something of their own — but if you each entered your own session, you each keep
  it, and the day is still together.
- **Or don't.** Two sessions in the same place starting within `together_window_min` minutes
  (default 30) are taken as the same outing. Set it to `0` to switch the guess off and rely only on
  the marker. `#apart` on either event settles it the other way, for a day that only looks shared.

The band carries the time and place once. If you're both doing the identical session it collapses
to one label with both names under it; if you're doing your own thing — or one of you has finished
and the other hasn't — each of you keeps a line.

```
SUN 27 SEP                             63 DAYS TO THE CITY 10K
───────────────────────────────────────────────────────────────
[TOGETHER]  06:30  ·  Riverside Park
ALEX    LONG RUN 14 KM                                   [DONE]
SAM     EASY RUN 6 KM
───────────────────────────────────────────────────────────────
```

`render.py --fixture fixtures/together-events.json` renders one.

## Emoji

People put emoji in calendar events, and neither display face has a single symbol glyph — not
even `♥` — so one used to come out as a `.notdef` box. Any character the text face can't draw is
looked up in **Noto Emoji** (monochrome, OFL) and drawn from there; anything neither face has is
dropped, because a gap reads better on a wall than a box. If Noto Emoji isn't installed, every
emoji is simply dropped, so the fetch is optional.

Two details follow from a 1-bit screen with no colour:

- **Sequences collapse to their first picture**, since laying them out needs libraqm: 🏋️‍♀️ draws as
  🏋, 👍🏽 as 👍, 1️⃣ as `1`.
- **Hearts are redrawn solid.** Noto hatches the coloured hearts to stand in for a colour this
  screen hasn't got, which at 24 px is a smudge, so ❤️ 💙 💜 and the rest all draw as `♥`.

Emoji are kept verbatim in `today.json` — the substitutions are a property of the picture, not the data.

## Data formats (all optional)

- **Goals:** Markdown files with YAML frontmatter `status: active` and `target_date: YYYY-MM-DD`.
  The nearest future date becomes the countdown. Month-only dates are ignored.
- **Activity log:** a CSV with `date,type,name,...` and optionally `distance_km, pace_min_km, avg_hr,
  moving_time, location`. `type` of `run` or `weight_training` matches run/strength sessions.
- **Quotes:** a Markdown file; every line starting `- ` is a quote, ideally `- "text" — author`.
  One per day, rotating. A trailing `#rest` tag marks a quote for rest and mobility days only;
  untagged quotes are for training days. Each pool rotates on its own.
- **Special days:** `special_days` in the config replaces the quote on a date: `{"date": "MM-DD",
  "text": "...", "author": "...", "since": 2024}`. `{n}` / `{nth}` in the text become the years
  since `since` ("2" / "2nd"); `author` is optional; `"icon": "heart"` or `"cake"` draws one beside it.

## Tests

```
venv/bin/python -m unittest discover tests
```

## License

MIT. Fonts are under the SIL Open Font License and are downloaded, not vendored.
