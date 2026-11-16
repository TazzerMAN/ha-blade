"""Compose the three setup steps into one image per language and theme: python steps.py docs/images."""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
IMAGES = OUT = Path(sys.argv[1]).resolve()
PAGE = HERE / "_page.html"
STEPS = {
    "en": ["Find Blade", "Paste your API key", "Just ask"],
    "fr": ["Trouvez Blade", "Collez votre clé API", "Demandez, c’est fait"],
}
THEME = {
    "light": {"ink": "#171A1F", "muted": "#5C6470", "card": "#FFFFFF", "line": "#E4E2DB"},
    "dark": {"ink": "#FAFAF8", "muted": "#AEB6C0", "card": "#202833", "line": "#2C3440"},
}


def html(lang: str, theme: str) -> str:
    c = THEME[theme]
    cards = "".join(
        f"""<figure><figcaption><span class="n">{i + 1}</span>{title}</figcaption>
        <div class="shot{' crop' if i == 0 else ''}"><img src="{(IMAGES / lang / name).as_uri()}"></div></figure>"""
        for i, (title, name) in enumerate(zip(STEPS[lang], ["1-search.png", "2-setup.png", "4-assist.png"]))
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300..700&family=Onest:wght@100..900&family=Azeret+Mono:wght@100..900&display=block"><style>
* {{ margin: 0; box-sizing: border-box; }}
html, body {{ background: transparent; }}
body {{ width: 1200px; padding: 8px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 28px; align-items: start; font-family: 'Space Grotesk', sans-serif; }}
figure {{ display: flex; flex-direction: column; gap: 14px; }}
figcaption {{ display: flex; align-items: center; gap: 12px; font-size: 21px; font-weight: 600; color: {c['ink']}; }}
.n {{ display: inline-grid; place-items: center; width: 34px; height: 34px; border-radius: 50%; background: #F2B92E; color: #171A1F; font-size: 18px; }}
.shot {{ border-radius: 18px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,{0.10 if theme == 'light' else 0.35}); background: #fff; }}
.shot img {{ display: block; width: 100%; }}
.shot.crop {{ height: 150px; }}
</style></head><body>{cards}</body></html>"""


OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    for lang in STEPS:
        for theme in THEME:
            page = browser.new_page(viewport={"width": 1216, "height": 800}, device_scale_factor=2)
            PAGE.write_text(html(lang, theme))
            page.goto(PAGE.as_uri())
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(500)
            page.locator("body").screenshot(path=str(OUT / f"steps-{lang}-{theme}.png"), omit_background=True)
            PAGE.unlink()
            page.close()
            print("rendu", lang, theme)
    browser.close()
