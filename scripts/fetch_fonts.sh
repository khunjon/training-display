#!/usr/bin/env bash
# Download the display's fonts (SIL Open Font License) from the Google Fonts repo.
# Usage: scripts/fetch_fonts.sh [fonts_dir]   (default ~/.local/state/training-display/fonts)
set -euo pipefail
DIR="${1:-$HOME/.local/state/training-display/fonts}"
mkdir -p "$DIR"
for f in barlowcondensed/BarlowCondensed-Bold.ttf barlowcondensed/BarlowCondensed-SemiBold.ttf \
         ibmplexmono/IBMPlexMono-Regular.ttf ibmplexmono/IBMPlexMono-Medium.ttf ibmplexmono/IBMPlexMono-Bold.ttf; do
  curl -sSfL -o "$DIR/$(basename "$f")" "https://github.com/google/fonts/raw/main/ofl/$f"
  echo "fetched $(basename "$f")"
done
