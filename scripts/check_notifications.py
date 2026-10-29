"""Setup check: Telegram + Gmail SMTP/IMAP with secrets from config/secrets.env.
Never prints secret values. Writes TELEGRAM_CHAT_ID back to secrets.env when discovered."""
import imaplib, json, os, re, smtplib, ssl, sys, urllib.request
from email.message import EmailMessage
from pathlib import Path

ENV = Path(os.environ.get("AICORP_HOME") or Path(__file__).resolve().parent.parent) / "config" / "secrets.env"      # the same data folder the dashboard uses

def load():
    d = {}
    for l in ENV.read_text().splitlines():
        if l.strip() and not l.startswith("#") and "=" in l:
            k, _, v = l.partition("="); d[k.strip()] = v.strip()
    return d

def tg(tok, method, **p):
    data = json.dumps(p).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def save_chat_id(cid):
    t = ENV.read_text()
    t = re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={cid}", t, flags=re.M)
    ENV.write_text(t)

def telegram(c):
    try:
        me = tg(c["TELEGRAM_BOT_TOKEN"], "getMe")["result"]
        print(f"telegram getMe OK: bot @{me['username']}")
        cid = c.get("TELEGRAM_CHAT_ID")
        if not cid:
            ups = tg(c["TELEGRAM_BOT_TOKEN"], "getUpdates")["result"]
            chats = {u["message"]["chat"]["id"] for u in ups if "message" in u}
            if not chats:
                print(f"telegram: no messages yet -> send any message to @{me['username']} then re-run"); return False
            if len(chats) > 1:
                print("telegram: multiple chats found, not auto-selecting:", len(chats)); return False
            cid = str(chats.pop()); save_chat_id(cid); print("telegram chat id discovered and saved")
        tg(c["TELEGRAM_BOT_TOKEN"], "sendMessage", chat_id=cid, text="AICorp setup test: Telegram alert channel works.")
        print("telegram sendMessage OK"); return True
    except Exception as e:
        print("telegram FAILED:", type(e).__name__, str(e)[:120].replace(c.get("TELEGRAM_BOT_TOKEN", "#"), "***")); return False

def gmail(c):
    ok = True
    pw = re.sub(r"\s", "", c["GMAIL_APP_PASSWORD"])
    try:
        m = EmailMessage(); m["From"] = c["GMAIL_ADDRESS"]; m["To"] = c["ALERT_EMAIL_TO"]
        m["Subject"] = "AICorp setup test: email alert channel works"
        m.set_content("This is an automated test from your AICorp setup (setup check). No action needed.")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
            s.login(c["GMAIL_ADDRESS"], pw); s.send_message(m)
        print("gmail SMTP send OK")
    except Exception as e:
        ok = False; print("gmail SMTP FAILED:", type(e).__name__, str(e)[:160].replace(pw, "***"))
    try:
        im = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        im.login(c["GMAIL_ADDRESS"], pw); typ, data = im.select("INBOX", readonly=True)
        print("gmail IMAP login OK, INBOX messages:", data[0].decode()); im.logout()
    except Exception as e:
        ok = False; print("gmail IMAP FAILED:", type(e).__name__, str(e)[:160].replace(pw, "***"))
    return ok

if __name__ == "__main__":
    c = load()
    which = sys.argv[1:] or ["telegram", "gmail"]
    if "telegram" in which: telegram(c)
    if "gmail" in which: gmail(c)
