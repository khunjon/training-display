# Training Display — project instructions

A living-room e-paper screen answering "what are we training today?" — see `README.md` for the design and setup. This file is for working on the code.

## Where things are

- **Spec, decisions, budget, build status:** the vault note `~/Projects/life/ai/Projects/Training Display.md` — read it first in a new session; it is the source of truth for *what* this is and *why* each decision was made. Update its phase list and status line when a phase lands.
- **Quotes:** `~/Projects/life/ai/Projects/Training Display Quotes.md` (edited in Obsidian; this repo only reads it).
- **Local config and secrets (never in git):** `~/.config/training-display/config.json` and `sa.json` (the Google service-account key, mode 600).
- **Runtime state:** `~/.local/state/training-display/` — `venv/` (python 3.12 via `uv`; google-auth, requests, Pillow), `fonts/`, `today.png`, `today.json`, `events-*.json`.
- **Run everything with the venv:** `~/.local/state/training-display/venv/bin/python render.py`. Tests: `venv/bin/python -m unittest discover tests`.

## Rules

- **This repo is public and open source (MIT).** Nothing personal goes in it: no names, emails, calendar ids, vault paths or keys in code, fixtures, tests or commit messages. Everything of that kind lives in `config.json`. The README's example frame is the one deliberate exception.
- **The vault is data; this repo is code.** The renderer reads vault files by configured path and never writes to the vault. Output goes only to `output_dir`.
- **One screen, one question.** Today only, both people, countdown, quote. No readiness numbers, no verdict, no week view, no weather. If a feature needs a second page it does not belong here.
- **Health data stays on the LAN.** The server binds locally; nothing is published to the internet.
- **Never a blank wall.** A failed fetch re-renders the last good frame with a `(stale)` stamp.

## Phases (status lives in the vault note)

0. Shared calendar + service account — done. **Still open:** `push.py`, a CLI the vault's plan-changing skills call to write a session (title, date, optional time/location; default slots come from config).
1. `render.py` — done, tested.
2. `server.py` — done, tested. Stdlib, port 8787: `/today.png`, `/today.json`, `/health`, `/` (phone page), and the TRMNL protocol `/api/setup`, `/api/display` (`filename` = hash of the PNG, so the device only redraws on change), `/api/log`. `scripts/install_launchd.sh` generates and loads two agents: `<prefix>.training-display` (render, 06:00 then hourly to 21:00, RunAtLoad, optionally wrapped by `--guard`) and `<prefix>.training-display-server` (KeepAlive). Logs in `~/Library/Logs/training-display*.log`. Protocol details were read from the firmware source (`lib/trmnl/src/parse_response_api_display.cpp`, `request_headers.cpp`).
3. Flash the TRMNL firmware on the Seeed × TRMNL 7.5" kit, point it at the server, hang it.

## Conventions

- Plain Python, stdlib where possible; the three deps in `requirements.txt` are the ceiling unless there is a strong reason.
- Resolver logic gets a unit test; rendering gets a smoke test (mode `1`, 800×480). No network in tests.
- Commit messages end with the Claude co-author line the session supplies.
