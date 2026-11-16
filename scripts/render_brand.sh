#!/bin/sh
# Render the brand PNGs Home Assistant serves from custom_components/blade/brand.
# Sources: assets/brand/*.svg, drawn from the Blade "buddy" mascot of blade.sh.
set -eu
cd "$(dirname "$0")/.."
out=custom_components/blade/brand
mkdir -p "$out"
convert -background none -density 400 assets/brand/icon.svg -resize 256x256 "$out/icon.png"
convert -background none -density 400 assets/brand/icon.svg -resize 512x512 "$out/icon@2x.png"
for name in logo dark_logo; do
  convert -background none -density 300 "assets/brand/$name.svg" -resize x128 "$out/$name.png"
  convert -background none -density 300 "assets/brand/$name.svg" -resize x256 "$out/$name@2x.png"
done
