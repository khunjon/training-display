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

# Monochrome emoji, for whatever anyone types into a calendar event. Neither display face
# has a symbol glyph, so without this an emoji draws as a .notdef box. Optional: if it is
# missing the renderer drops emoji instead. The variable font's default instance is Regular.
curl -sSfL -o "$DIR/NotoEmoji-Regular.ttf" \
  "https://github.com/google/fonts/raw/main/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf" \
  && echo "fetched NotoEmoji-Regular.ttf" \
  || echo "NotoEmoji-Regular.ttf not fetched — emoji will be dropped from the frame" >&2

# Thai, a partner for each display face (both are Latin only). Static instances from the Noto
# project, since Google Fonts ships only the variable font. Optional like the emoji face:
# without them Thai is dropped from the frame.
for w in CondensedBold CondensedSemiBold Medium Bold; do
  curl -sSfL -o "$DIR/NotoSansThai-$w.ttf" \
    "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSansThai/hinted/ttf/NotoSansThai-$w.ttf" \
    && echo "fetched NotoSansThai-$w.ttf" \
    || echo "NotoSansThai-$w.ttf not fetched — Thai will be dropped from the frame" >&2
done
