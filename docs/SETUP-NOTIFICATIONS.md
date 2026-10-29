# Set up alerts and chat (Telegram and Gmail)

Optional. With Telegram or Gmail connected, AICorp's Advisor can warn you when something needs you and answer short questions from your phone.
Everything works without them (the dashboard still shows every alert).

| You will need | Time |
|---|---|
| A Telegram account (phone app) | about 5 minutes |
| A Google account you control (not a company or school account, see the notes) | about 10 minutes |

## Golden rules (read first)

1. **A token or password is like a house key.** Type it **only** into the AICorp dashboard (Settings -> Credentials). Never paste it into a chat, an issue, an e-mail, a screenshot, a document or a terminal history.
2. **Do not send a screenshot of a page that shows a token.** Telegram's BotFather chat and Google's app-password page both show the secret once.
3. **If a secret ever leaks, replace it right away** (see *If a secret leaks* below). It takes two minutes.
4. AICorp stores the values in a private file, never shows them again, removes them from logs and backups, and only accepts messages from **your** Telegram chat and **your** e-mail address. Anyone else is ignored and logged.

## Part 1: Telegram

### 1. Create your bot
1. In Telegram, search for **@BotFather** (the official bot-maker; check for the blue verified tick) and open it.
2. Send `/newbot`.
3. Give the bot a display name (anything), then a username that **ends in `bot`** and is not taken.
4. BotFather replies with a **token** (numbers, a colon, then letters and digits). Keep that message private.

### 2. Save the token in AICorp
1. Open the dashboard, **Settings -> Credentials**.
2. Paste the token into **Telegram bot token** (the box hides what you type) and press **Save credentials**.
3. Delete the BotFather message afterwards if you like; you can always get a new token later.

### 3. Let AICorp learn your chat
1. In Telegram, open your new bot and send it any message, for example `hello`.
2. Find your chat ID automatically (this runs on your computer; the token never leaves it except to Telegram):
   `python scripts/check_notifications.py telegram` (in the AICorp folder, inside Linux/WSL).
3. It prints `telegram chat id discovered and saved` and sends a test message to your bot.
   If it says there are several chats, remove old chats with the bot and try again.

Do **not** open `api.telegram.org/bot<token>/...` links in a browser to find the ID: that puts your token into the browser history.

### 4. Check it
Dashboard -> **Settings** -> send a test to Telegram. You should get a message within a few seconds. Then send `/help` to your bot.

## Part 2: Gmail

AICorp uses a Google **app password**, never your normal Gmail password.

1. Turn on **2-Step Verification** for the Google account (Google Account -> Security). The app-password option only exists with it.
2. Open **https://myaccount.google.com/apppasswords** while signed in, name it `AICorp`, and press **Create**. Google shows a 16-character password once.
3. Dashboard -> **Settings -> Credentials**: fill **Gmail address**, **Gmail app password** (spaces do not matter), and **Send alerts to** (can be the same address). **Save credentials**.
4. If your Gmail settings still show an IMAP switch, make sure IMAP is **on** (Settings -> See all settings -> Forwarding and POP/IMAP). Newer Gmail may not show the switch because IMAP is always on.
5. Test: Settings -> send a test to e-mail, or run `python scripts/check_notifications.py gmail`.

To talk to the Advisor by e-mail, send mail from your own address with `[AICorp]` in the subject.

## If a secret leaks

| What leaked | What to do |
|---|---|
| Telegram token | In Telegram open BotFather, send `/revoke`, choose your bot; it issues a new token and the old one stops working. Save the new one in Settings. |
| Gmail app password | Google Account -> Security -> App passwords: delete the `AICorp` entry and create a new one. (Changing your Google password also cancels all app passwords.) |
| Either, and you pasted it somewhere public | Replace it first, then delete the place where you pasted it. Deleting alone is not enough: assume it was copied. |

## Troubleshooting

| Problem | Likely cause |
|---|---|
| The Telegram test fails | Wrong or old token (copy the newest from BotFather); the watchdog is not running; no internet |
| `no messages yet` | You have not sent your bot a message yet |
| No app-password page at Google | 2-Step Verification is off, the account is a work/school account, or Advanced Protection is on. A personal account with 2-Step Verification is the safe choice |
| Gmail test: authentication failed | A normal password was used instead of the app password; the app password was deleted; the Google password was changed afterwards |
| Alerts arrive but chat does not answer | Questions are answered while the task engine is running |

## Honest notes

* These steps follow how Telegram and Google work today. Both change their screens now and then; if a name differs, look for the closest one. The Telegram and Google steps were not taken from their official documentation pages in this version of the guide, so check them against what you see.
* Whether Google Workspace (company or school) accounts offer app passwords depends on the administrator; AICorp has been tested only with a personal Gmail account.
* Telegram messages pass through Telegram's servers and e-mail through Google's; do not send secrets through either.
