#!/bin/sh
# Render every README image: screenshots of a blank Home Assistant per language, the setup steps and the banners.
# Needs BLADE_API_KEY, ImageMagick, and Playwright's Chromium (uv run --with playwright==1.56.0 playwright install chromium).
set -eu
: "${BLADE_API_KEY:?set BLADE_API_KEY}"
cd "$(dirname "$0")/../.."
here=scripts/readme
images=docs/images
work=$(mktemp -d)
trap 'kill "$(cat "$work/hass.pid" 2>/dev/null)" 2>/dev/null || true; rm -rf "$work"' EXIT
py() { uv run --with playwright==1.56.0 python "$@"; }

for lang in en fr; do
  mkdir -p "$work/$lang/config/custom_components" "$work/$lang/raw"
  cp "$here/demo-$lang.yaml" "$work/$lang/config/configuration.yaml"
  ln -s "$PWD/custom_components/blade" "$work/$lang/config/custom_components/blade"
  uv run hass -c "$work/$lang/config" > "$work/$lang/hass.log" 2>&1 &
  echo $! > "$work/hass.pid"
  until curl -s -o /dev/null http://127.0.0.1:8123/api/onboarding; do sleep 2; done
  py "$here/screenshots.py" "$lang" "$work/$lang/raw"
  kill "$(cat "$work/hass.pid")"
  while kill -0 "$(cat "$work/hass.pid")" 2>/dev/null; do sleep 1; done

  # Dialogs lose their 24 px margin and get transparent 48 px corners.
  for name in 1-search 2-setup 4-assist 5-options; do
    raw="$work/$lang/raw/$name.png"
    w=$(identify -format %w "$raw"); h=$(identify -format %h "$raw")
    convert "$raw" -crop $((w - 48))x$((h - 48))+24+24 +repage \
      \( +clone -alpha extract -draw "fill black polygon 0,0 0,48 48,0 fill white circle 48,48 48,0" \
         \( +clone -flip \) -compose Multiply -composite \( +clone -flop \) -compose Multiply -composite \) \
      -alpha off -compose CopyOpacity -composite -strip "$images/$lang/$name.png"
  done
  convert "$work/$lang/raw/3-services.png" -crop 2560x1110+0+0 +repage -strip "$images/$lang/3-services.png"
done

py "$here/steps.py" "$images"
py "$here/banner.py" "$images"
