# Weekly grocery list bot

Generates a 7-day meal plan (mostly Karnataka/South Indian, with other cuisines in
rotation) shaped by your health goals, turns it into one consolidated grocery list, and
emails it to you every Saturday — ready for Sunday-morning shopping. Optional WhatsApp
send too. Runs entirely on free tiers: **$0/month.**

## How it works

- **Scheduling**: a GitHub Actions workflow, free for scheduled jobs like this.
- **Meal planning + grocery list**: Google Gemini API (free tier — no credit card needed).
- **Email**: your own Gmail account via SMTP (free).
- **WhatsApp (optional)**: CallMeBot's free personal WhatsApp API.
- **Memory**: `meal_history.json` tracks recent weeks so meals don't repeat, and
  `pantry.json` tracks staples you already have so they're not re-added every week; the
  workflow commits both back to the repo after each run.

## One-time setup (about 20 minutes)

### 1. Put this folder in a GitHub repo
Create a **private** repo (private is fine and free) and push this folder to it.

### 2. Get a free Gemini API key
Go to [Google AI Studio](https://aistudio.google.com/), sign in, click "Get API key,"
create a key. No credit card required for the free tier.

### 3. Create a Gmail App Password
Regular Gmail passwords won't work for SMTP. Instead:
1. Turn on 2-Step Verification on your Google account, if not already on.
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it e.g. "grocery-bot") and copy the 16-character code.

### 4. Add secrets to your GitHub repo
In your repo: **Settings → Secrets and variables → Actions → New repository secret.**
Add:

| Secret name | Value |
|---|---|
| `GEMINI_API_KEY` | the key from step 2 |
| `GMAIL_ADDRESS` | the Gmail address you'll send *from* |
| `GMAIL_APP_PASSWORD` | the 16-character app password from step 3 |
| `RECIPIENT_EMAIL` | the address you want the list sent *to* (can be the same address) |

### 5. (Optional) Set up free WhatsApp delivery
CallMeBot is an unofficial, free, hobbyist API for sending yourself WhatsApp messages.
It's rate-limited and meant for personal use, not production — fine for this.
1. Save `+34 644 59 71 20` as a contact on your phone.
2. Send it a WhatsApp message: `I allow callmebot to send me messages`
3. You'll get an API key back. Add two more repo secrets: `CALLMEBOT_PHONE` (your number,
   with country code, no `+`) and `CALLMEBOT_APIKEY`.
4. If you skip this, the script just skips WhatsApp and only sends email — that's fine.

### 6. Edit `preferences.json` and `pantry.json`
`preferences.json` has a `people` list — one entry per household member with their own
`goals` and `dietary_restrictions` — plus shared settings like cuisine mix and how many
past weeks to avoid repeating. The AI defaults to dishes that work for whoever has the
tighter restriction, and treats anything only some people can eat (e.g. meat) as an
optional add-on portion rather than planning separate meals.

`pantry.json` is currently seeded from your real 102-item pantry list (exported from the
"Current Pantry Items" tab of your Personal Butler sheet). It's split into:
- `always_stocked` — excluded from every grocery list.
- `low_stock` — anything flagged as needing a top-up; shows up on the next list even
  though it's normally a staple, then auto-clears after that run.

To keep it current: whenever your real pantry list changes meaningfully, re-export that
sheet tab as CSV and send it over and it'll get rebuilt the same way. For quick one-off
exceptions ("running low on toor dal this week"), just add the item to `low_stock`
directly — no need to re-export anything.

**If you'd rather this stay in sync automatically:** the sheet itself currently isn't
reachable by the bot (I hit a permissions error trying to fetch it). If you set its
sharing to "Anyone with the link → Viewer," I can add a step that pulls the live sheet
each Saturday instead of relying on you to re-send exports — let me know and I'll wire
that in.

### 7. Test it manually before trusting the schedule
In the repo: **Actions tab → "Weekly grocery list" → Run workflow.** This uses the same
`workflow_dispatch` trigger as the Saturday cron, so it's a true test of the real path.
Check your email (and WhatsApp, if set up).

## Adjusting the schedule

The cron line in `.github/workflows/weekly-grocery.yml` is in UTC:
```yaml
- cron: "0 14 * * 6"   # Saturday 14:00 UTC
```
`14 * * 6` = hour 14, any day-of-month, any month, day-of-week 6 (Saturday). Change the
hour to shift when it lands in your day — just remember it's UTC, so account for the
offset to Pacific time (and that the offset itself shifts with daylight saving).

## Running it locally (without GitHub Actions)

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...
export GMAIL_ADDRESS=...
export GMAIL_APP_PASSWORD=...
export RECIPIENT_EMAIL=...
python plan_and_send.py
```

## Notes on the free tiers

- **Gemini free tier**: generous for this use case — you're making one request a week.
  If you ever hit a rate limit, it's almost certainly from testing repeatedly in a short
  window, not from the weekly production run.
- **GitHub Actions**: scheduled workflows on a personal repo are free within GitHub's
  standard free minutes, which are far more than one run a week will ever use.
- **CallMeBot**: free but unofficial and rate-limited — a fine fit for one message a
  week to yourself, not something to build a business on.

## Ideas for later, if you want to extend it

- Ask Gemini to also output a rough calorie/protein estimate per day against your goals.
- Add a second weekday reminder (e.g. Wednesday) with just what's left to cook that week.
- Extend `pantry.json` beyond dry staples to semi-perishables you tend to always have
  (e.g. onions, garlic, ginger) if that matches your actual shopping habits.
