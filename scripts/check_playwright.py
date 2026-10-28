"""Setup check: headless Chromium via Playwright works, CAPTCHA/login heuristic, RSS memory."""
import asyncio, subprocess
from playwright.async_api import async_playwright

CAPTCHA_HINTS = ["captcha", "i'm not a robot", "verify you are human", "cf-challenge", "recaptcha", "hcaptcha"]
LOGIN_HINTS = ["sign in", "log in", "password"]

def classify(html: str) -> str:
    h = html.lower()
    if any(k in h for k in CAPTCHA_HINTS): return "CAPTCHA"
    if h.count("type=\"password\"") or ("password" in h and "sign in" in h): return "LOGIN_REQUIRED"
    return "OK"

def rss_mb(name="headless_shell"):
    out = subprocess.run(["ps", "-eo", "rss,comm,args"], capture_output=True, text=True).stdout
    return round(sum(int(l.split()[0]) for l in out.splitlines()[1:] if name in l) / 1024)

async def main():
    pages = {
        "normal": "<html><body><h1>Hello</h1><p>article text</p></body></html>",
        "captcha": "<html><body><div class='g-recaptcha'>Verify you are human</div></body></html>",
        "login": "<html><body><form>Sign in <input type='password'></form></body></html>",
    }
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context()
        for k, html in pages.items():
            pg = await ctx.new_page()
            await pg.set_content(html)
            print(k, "->", classify(await pg.content()))
        print("RSS MB (chromium processes, 3 pages):", rss_mb())
        await b.close()

asyncio.run(main())
