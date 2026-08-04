"""
Weekly grocery list bot.

What it does, every time it runs:
1. Reads preferences.json (health goals, cuisine mix, restrictions).
2. Reads meal_history.json (what was cooked in recent weeks, so the AI avoids repeats).
3. Asks Gemini for a 7-day meal plan + a consolidated grocery list.
4. Emails the plan to you.
5. Optionally sends a short WhatsApp message via CallMeBot (free, unofficial).
6. Appends this week's dishes to meal_history.json so future weeks stay varied.

Meant to be triggered by the GitHub Actions workflow in .github/workflows/weekly-grocery.yml
every Saturday, but you can also just run it locally with `python plan_and_send.py` to test.
"""

import json
import os
import re
import smtplib
import sys
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from google import genai
from google.genai import types

BASE_DIR = Path(__file__).parent
PREFERENCES_FILE = BASE_DIR / "preferences.json"
HISTORY_FILE = BASE_DIR / "meal_history.json"


# ---------- 1. Load config ----------

def load_preferences() -> dict:
    with open(PREFERENCES_FILE, "r") as f:
        return json.load(f)


def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)


def recent_dishes(history: list, weeks: int) -> list:
    """Flatten dish names from the last `weeks` entries so we can tell the AI what to avoid."""
    dishes = []
    for week in history[-weeks:]:
        for day in week.get("days", []):
            for meal in ("breakfast", "lunch", "dinner"):
                if day.get(meal):
                    dishes.append(day[meal])
    return dishes


# ---------- 2. Build the prompt and call Gemini ----------

def build_prompt(preferences: dict, avoid_dishes: list, week_start: date) -> str:
    meals = preferences.get("meals_per_day", ["breakfast", "lunch", "dinner"])
    day_names = [(week_start + timedelta(days=i)).strftime("%A") for i in range(7)]

    avoid_text = (
        "Avoid repeating these dishes from recent weeks: " + ", ".join(avoid_dishes)
        if avoid_dishes else "No recent history yet — any dishes are fine."
    )

    return f"""
You are a meal-planning and grocery-shopping assistant for a household of {preferences.get('people', 2)}.

Health goals: {preferences.get('health_goals')}
Dietary restrictions: {preferences.get('dietary_restrictions')}
Cuisine mix: {preferences.get('cuisine_mix')}
{avoid_text}

Plan meals for these {len(day_names)} days: {", ".join(day_names)}.
Include these meals each day: {", ".join(meals)}.

Then produce a single consolidated grocery list covering everything needed to cook all of
these meals for the household size given, with realistic quantities (combine repeated
ingredients across meals into one line, e.g. if 3 dishes need onions, give one total).
Group the grocery list into sensible categories (Vegetables, Fruits, Grains & Pulses,
Dairy & Dairy Alternatives, Spices & Condiments, Other).

Respond with ONLY valid JSON, no markdown fences, matching exactly this shape:

{{
  "week_start": "{week_start.isoformat()}",
  "days": [
    {{"day": "{day_names[0]}", "breakfast": "...", "lunch": "...", "dinner": "..."}}
    // one object per day, in order
  ],
  "grocery_list": [
    {{"category": "Vegetables", "items": [{{"name": "Onion", "quantity": "1 kg"}}]}}
    // one object per category
  ],
  "notes": "any short caveats, e.g. assumed pantry staples like salt/oil are already on hand"
}}
""".strip()


def call_gemini(prompt: str) -> dict:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    raw = response.text.strip()
    # Safety net in case the model wraps the JSON in a code fence anyway.
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ---------- 3. Format the message ----------

def format_email_body(plan: dict) -> str:
    lines = [f"Meal plan & grocery list — week of {plan.get('week_start')}", ""]

    lines.append("MEAL PLAN")
    lines.append("-" * 40)
    for day in plan.get("days", []):
        lines.append(f"{day.get('day')}:")
        for meal in ("breakfast", "lunch", "dinner"):
            if day.get(meal):
                lines.append(f"  {meal.capitalize()}: {day[meal]}")
        lines.append("")

    lines.append("GROCERY LIST")
    lines.append("-" * 40)
    for category in plan.get("grocery_list", []):
        lines.append(f"{category.get('category')}:")
        for item in category.get("items", []):
            lines.append(f"  - {item.get('name')}: {item.get('quantity')}")
        lines.append("")

    if plan.get("notes"):
        lines.append(f"Notes: {plan['notes']}")

    return "\n".join(lines)


def format_whatsapp_body(plan: dict) -> str:
    """Shorter version — just the grocery list, since WhatsApp messages should stay brief."""
    lines = [f"Grocery list — week of {plan.get('week_start')}", ""]
    for category in plan.get("grocery_list", []):
        lines.append(f"*{category.get('category')}*")
        for item in category.get("items", []):
            lines.append(f"- {item.get('name')}: {item.get('quantity')}")
    return "\n".join(lines)


# ---------- 4. Send it ----------

def send_email(subject: str, body: str):
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", gmail_address)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [recipient], msg.as_string())


def send_whatsapp(body: str):
    """Optional. Uses CallMeBot's free personal WhatsApp API.
    Skipped automatically if the secrets aren't set — see README for setup.
    """
    phone = os.environ.get("CALLMEBOT_PHONE")
    api_key = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not api_key:
        print("CallMeBot credentials not set — skipping WhatsApp send.")
        return

    import urllib.parse
    import requests

    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={phone}&apikey={api_key}&text={urllib.parse.quote(body)}"
    )
    resp = requests.get(url, timeout=30)
    print(f"CallMeBot response: {resp.status_code} {resp.text[:200]}")


# ---------- 5. Update history ----------

def update_history(history: list, plan: dict, keep_weeks: int) -> list:
    history.append({"week_start": plan.get("week_start"), "days": plan.get("days", [])})
    history = history[-keep_weeks:] if keep_weeks else history
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    return history


# ---------- main ----------

def main():
    preferences = load_preferences()
    history = load_history()
    avoid = recent_dishes(history, preferences.get("weeks_of_history_to_avoid_repeating", 3))

    # Shopping happens the day after this runs (Saturday run -> Sunday start).
    week_start = date.today() + timedelta(days=1)

    prompt = build_prompt(preferences, avoid, week_start)
    plan = call_gemini(prompt)

    email_body = format_email_body(plan)
    send_email(subject=f"Grocery list — week of {plan.get('week_start')}", body=email_body)
    print("Email sent.")

    send_whatsapp(format_whatsapp_body(plan))

    keep_weeks = max(preferences.get("weeks_of_history_to_avoid_repeating", 3), 1) + 1
    update_history(history, plan, keep_weeks)
    print("History updated.")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        print(f"Missing required environment variable/secret: {e}", file=sys.stderr)
        sys.exit(1)
