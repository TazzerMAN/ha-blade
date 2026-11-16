"""Screenshots of a fresh Blade install, in one language, from a blank Home Assistant on port 8123.

BLADE_API_KEY=... python screenshots.py fr /path/to/raw/fr
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

LANG, OUT = sys.argv[1], sys.argv[2]
B = "http://127.0.0.1:8123"
KEY = os.environ["BLADE_API_KEY"]
T = {
    "en": {
        "user": "Alex", "username": "Username", "password": "Password",
        "add": "Add integration", "key": "API key", "submit": "Submit",
        "skip": "Skip and finish", "assist_q1": "Turn on the living room lamp, please.",
        "assist_q2": "What can you do for me, in one sentence?",
        "options": "Monthly spending cap (estimated)",
    },
    "fr": {
        "user": "Claire", "username": "Nom d'utilisateur", "password": "Mot de passe",
        "add": "Ajouter une intégration", "key": "Clé API", "submit": "Valider",
        "skip": "Ignorer et terminer", "assist_q1": "Allume la lampe du salon, s'il te plaît.",
        "assist_q2": "Qu'est-ce que tu sais faire pour moi, en une phrase ?",
        "options": "Plafond de dépense mensuel (estimé)",
    },
}[LANG]


def post(path, data, form=False, token=None):
    body = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return json.load(urllib.request.urlopen(urllib.request.Request(B + path, body, headers=headers)))


r = post("/api/onboarding/users", {"client_id": B + "/", "name": T["user"], "username": "demo", "password": "demo-blade-2026", "language": LANG})
token = post("/auth/token", {"grant_type": "authorization_code", "code": r["auth_code"], "client_id": B + "/"}, form=True)["access_token"]
for step, data in (("core_config", {}), ("analytics", {}), ("integration", {"client_id": B + "/", "redirect_uri": B + "/?auth_callback=1"})):
    post("/api/onboarding/" + step, data, token=token)


DIALOG = object()


def shot(page, name, wait=1500, clip=None):
    page.wait_for_timeout(wait)
    if clip is DIALOG:
        box = page.evaluate("""() => {
            const walk = (root) => {
                for (const el of root.querySelectorAll('*')) {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    if (s.borderTopLeftRadius !== '0px' && s.backgroundColor !== 'rgba(0, 0, 0, 0)'
                        && r.width > 500 && r.width < 800 && r.height > 200) return r.toJSON();
                    if (el.shadowRoot) { const f = walk(el.shadowRoot); if (f) return f; }
                }
                return null;
            };
            return walk(document);
        }""")
        clip = {"x": box["x"] - 12, "y": box["y"] - 12, "width": box["width"] + 24, "height": box["height"] + 24} if box else None
    page.screenshot(path=f"{OUT}/{name}.png", clip=clip)
    print("capture", name, clip)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 860}, locale="en-US" if LANG == "en" else "fr-FR", device_scale_factor=2)
    page.goto(B + "/")
    page.wait_for_timeout(2500)
    page.get_by_role("textbox", name=T["username"]).fill("demo")
    page.get_by_role("textbox", name=T["password"]).fill("demo-blade-2026")
    page.keyboard.press("Enter")
    page.wait_for_url("**/home/**", timeout=30000)

    page.goto(B + "/config/integrations/dashboard")
    page.wait_for_timeout(3000)
    page.get_by_role("button", name=T["add"]).click()
    page.wait_for_timeout(2500)
    page.keyboard.type("Blade", delay=60)
    shot(page, "1-search", clip=DIALOG)
    page.get_by_text("Blade", exact=True).first.click()
    page.wait_for_timeout(3500)
    shot(page, "2-setup", clip=DIALOG)
    page.get_by_role("textbox", name=T["key"]).fill(KEY)
    page.get_by_role("button", name=T["submit"]).click()
    page.wait_for_timeout(7000)
    page.get_by_role("button", name=T["skip"]).click()
    page.wait_for_timeout(2000)

    page.goto(B + "/config/integrations/integration/blade")
    shot(page, "3-services", 4000)

    page.goto(B + "/home/overview")
    page.wait_for_timeout(3500)
    page.evaluate("""async () => {
        const hass = document.querySelector("home-assistant").hass;
        const list = await hass.callWS({type: "assist_pipeline/pipeline/list"});
        const blade = list.pipelines.find((p) => p.name === "Blade");
        await hass.callWS({type: "assist_pipeline/pipeline/set_preferred", pipeline_id: blade.id});
    }""")
    page.reload()
    page.wait_for_timeout(3500)
    page.keyboard.press("a")
    page.wait_for_timeout(2500)
    page.keyboard.type(T["assist_q1"], delay=20)
    page.keyboard.press("Enter")
    page.wait_for_timeout(6000)
    page.keyboard.type(T["assist_q2"], delay=20)
    page.keyboard.press("Enter")
    shot(page, "4-assist", 7000, clip=DIALOG)
    page.keyboard.press("Escape")

    page.goto(B + "/config/integrations/integration/blade")
    page.wait_for_timeout(3500)
    page.mouse.click(1158, 443)
    page.set_viewport_size({"width": 1280, "height": 1100})
    shot(page, "5-options", 3000, clip=DIALOG)
    browser.close()

