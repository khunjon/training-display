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
quotes.md                 ┘        │            (launchd/cron: hourly)
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
- **Done state comes from a local activity log**, never from writing back to the calendar. If
  something was logged that wasn't planned, the log wins and it still shows as done.
- **LAN only.** Nothing leaves the house; health data stays local.
- If the calendar is unreachable, the last good frame is re-rendered with a `(stale)` stamp — never a blank wall.

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
3. **Fonts.** `scripts/fetch_fonts.sh` downloads Barlow Condensed and IBM Plex Mono (OFL) into `fonts_dir`.
4. **Python.** `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
5. **Render.** `venv/bin/python render.py` → `today.png`. Try `--date 2026-09-21`, or
   `--fixture fixtures/sample-events.json` to render without a calendar.
6. **Serve.** `venv/bin/python server.py` binds `0.0.0.0:8787` (LAN only — put it behind nothing
   that forwards from the internet). Routes: `/` (phone page), `/today.png`, `/today.json`, `/health`,
   and the TRMNL protocol `/api/setup`, `/api/display`, `/api/log`. An optional `"server"` block in
   the config sets `bind`, `port`, `refresh_rate` (device sleep, seconds) and `image_url` (only if the
   device cannot reuse the host it reached you on, e.g. behind a proxy).
7. **Schedule (macOS).** `scripts/install_launchd.sh` installs two launchd agents: the renderer at
   06:00 then hourly to 21:00 (and on load), and the server kept alive. `--guard path/to/wrapper.sh`
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

## Data formats (all optional)

- **Goals:** Markdown files with YAML frontmatter `status: active` and `target_date: YYYY-MM-DD`.
  The nearest future date becomes the countdown. Month-only dates are ignored.
- **Activity log:** a CSV with `date,type,name,...` and optionally `distance_km, pace_min_km, avg_hr,
  moving_time, location`. `type` of `run` or `weight_training` matches run/strength sessions.
- **Quotes:** a Markdown file; every line starting `- ` is a quote, ideally `- "text" — author`.
  One per day, rotating.
- **Special days:** `special_days` in the config replaces the quote on a date: `{"date": "MM-DD",
  "text": "...", "author": "...", "since": 2024}`. `{n}` / `{nth}` in the text become the years
  since `since` ("2" / "2nd"); `author` is optional; `"icon": "heart"` draws a heart beside it.

## Tests

```
venv/bin/python -m unittest discover tests
```

## License

MIT. Fonts are under the SIL Open Font License and are downloaded, not vendored.
