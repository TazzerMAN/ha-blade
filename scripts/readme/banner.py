"""Render the README banners and the social preview in Blade's brand: python banner.py docs/images."""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
PAGE = HERE / "_page.html"
OUT = Path(sys.argv[1])
ICON = (HERE.parent.parent / "assets/brand/icon.svg").read_text()
MASCOT = re.search(r"<svg[^>]*>(.*)</svg>", ICON, re.S).group(1)
VIEWBOX = re.search(r'viewBox="([^"]+)"', ICON).group(1)

TEXT = {
    "en": {
        "kicker": "Home Assistant integration",
        "title": "Blade for Home Assistant",
        "tagline": "Hosted in Europe. Operated by Shadow.",
        "chips": ["Assist", "Voice", "AI tasks", "Evaluation", "Forecast", "Spend control"],
        "foot": "Open-weight models · OpenAI-compatible API · Unofficial integration",
    },
    "fr": {
        "kicker": "Intégration Home Assistant",
        "title": "Blade pour Home Assistant",
        "tagline": "Hébergé en Europe. Exploité par Shadow.",
        "chips": ["Assist", "Voix", "Tâches IA", "Évaluation", "Prévision", "Dépense maîtrisée"],
        "foot": "Modèles ouverts · API compatible OpenAI · Intégration non officielle",
    },
}
THEME = {
    "light": {"bg": "#FAFAF8", "ink": "#171A1F", "muted": "#5C6470", "line": "#E4E2DB", "chip": "#FFFDF8", "glow": "rgba(242,185,46,0.22)"},
    "dark": {"bg": "#1A2029", "ink": "#FAFAF8", "muted": "#AEB6C0", "line": "#2C3440", "chip": "#202833", "glow": "rgba(242,185,46,0.16)"},
}


def page_html(lang: str, theme: str, width: int, height: int, social: bool) -> str:
    t, c = TEXT[lang], THEME[theme]
    chips = "".join(f"<span>{chip}</span>" for chip in t["chips"])
    mascot = 360 if social else 250
    return f"""<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300..700&family=Onest:wght@100..900&family=Azeret+Mono:wght@100..900&display=block"><style>
* {{ margin: 0; box-sizing: border-box; }}
body {{ width: {width}px; height: {height}px; background: {c['bg']}; color: {c['ink']}; font-family: Onest, sans-serif;
  position: relative; overflow: hidden; display: flex; align-items: center; gap: {72 if social else 64}px; padding: 0 {88 if social else 72}px; }}
body::before {{ content: ""; position: absolute; inset: 0;
  background-image: linear-gradient({c['line']} 1px, transparent 1px), linear-gradient(90deg, {c['line']} 1px, transparent 1px);
  background-size: 40px 40px; opacity: .55; mask-image: radial-gradient(ellipse at 22% 50%, black 0%, transparent 70%); }}
.glow {{ position: absolute; left: {8 if social else 4}%; top: 50%; width: {mascot * 2}px; height: {mascot * 2}px;
  transform: translate(-25%, -50%); background: radial-gradient(circle, {c['glow']} 0%, transparent 62%); }}
.mascot {{ position: relative; width: {mascot}px; flex: none; }}
.text {{ position: relative; display: flex; flex-direction: column; gap: {22 if social else 16}px; }}
.kicker {{ font-family: 'Azeret Mono', monospace; font-size: {17 if social else 14}px; letter-spacing: .14em; text-transform: uppercase; color: {c['muted']}; }}
h1 {{ font-family: 'Space Grotesk', sans-serif; font-weight: 600; font-size: {68 if social else 54}px; line-height: 1.02; letter-spacing: -.02em; }}
.tagline {{ font-size: {30 if social else 24}px; font-weight: 500; }}
.tagline b {{ {'background: linear-gradient(transparent 58%, #F2B92E66 58%);' if theme == 'light' else 'color: #FFD35C;'} font-weight: 600; }}
.chips {{ display: flex; flex-wrap: wrap; gap: {8 if social else 10}px; margin-top: 6px; max-width: {700 if social else 760}px; }}
.chips span {{ font-family: 'Azeret Mono', monospace; font-size: 13px; padding: {6 if social else 7}px {11 if social else 13}px; border: 1px solid {c['line']};
  border-radius: 999px; background: {c['chip']}; color: {c['ink']}; }}
.foot {{ position: absolute; right: {88 if social else 72}px; bottom: {40 if social else 26}px; font-size: {16 if social else 13}px; color: {c['muted']}; }}
</style></head><body>
<div class="glow"></div>
<svg class="mascot" xmlns="http://www.w3.org/2000/svg" viewBox="{VIEWBOX}">{MASCOT}</svg>
<div class="text">
  <div class="kicker">{t['kicker']}</div>
  <h1>{t['title']}</h1>
  <div class="tagline"><b>{t['tagline'].split('. ')[0]}.</b> {t['tagline'].split('. ', 1)[1]}</div>
  <div class="chips">{chips}</div>
</div>
<div class="foot">{t['foot']}</div>
</body></html>"""


OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    jobs = [(lang, theme, 1280, 400, False, f"banner-{lang}-{theme}.png") for lang in TEXT for theme in THEME]
    jobs.append(("en", "light", 1280, 640, True, "social-preview.png"))
    for lang, theme, w, h, social, name in jobs:
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2 if not social else 1)
        PAGE.write_text(page_html(lang, theme, w, h, social))
        page.goto(PAGE.as_uri())
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(OUT / name))
        page.close()
        PAGE.unlink()
        print("rendu", name)
    browser.close()
