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


def clean_secret(value: str) -> str:
    """Strip whitespace and invisible characters (e.g. non-breaking spaces, \\xa0) that
    sometimes get carried along when copy-pasting secrets like Gmail App Passwords from a
    web page into GitHub Secrets. Also drops internal spaces, since Gmail displays the App
    Password in space-separated groups but the real value has none."""
    return "".join(value.split())


_SYNONYM_PHRASES = {
    # A conservative, curated list — only unambiguous 1:1 naming variants, applied as
    # whole-phrase substitution before tokenization. Deliberately excludes anything with
    # real ambiguity (e.g. broad "chickpeas" is NOT aliased to "chana dal" — whole
    # chickpeas/garbanzo and split chana dal are genuinely different products).
    "chana dal": "bengal gram split",
    "channa dal": "bengal gram split",
    "cilantro": "coriander",
}


def normalize_item_name(s: str) -> frozenset:
    """Normalize an ingredient name for matching pantry entries against grocery-list items.
    Strips a single trailing "(...)" annotation (used for local-language names, e.g.
    "Toor Dal (Togari Bele)" -> "Toor Dal"), applies a small curated synonym list for common
    naming variants (e.g. "Chana Dal" -> "Bengal Gram Split"), then returns a set of
    lowercased, singularized words rather than a literal string — so "Green Gram (Whole)"
    and "Whole Green Gram" match despite differing word order. Deliberately does NOT strip
    other qualifiers like "(Split)" or "(Whole)" earlier in the name — those distinguish
    genuinely different products, and as a differing word they correctly break the match
    (e.g. "Green Gram (Split Skinless)" vs. "Whole Green Gram" share only 2 of 3-4 words,
    so they won't match)."""
    s = re.sub(r"\s*\([^()]*\)\s*$", "", s)
    s = s.lower().strip()
    for phrase, canonical in _SYNONYM_PHRASES.items():
        s = s.replace(phrase, canonical)
    s = re.sub(r"[^\w\s]", " ", s)
    words = []
    for word in s.split():
        if word.endswith("es") and len(word) > 3:
            word = word[:-2]
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 2:
            word = word[:-1]
        words.append(word)
    return frozenset(words)


def resolve_pantry(pantry: dict):
    """Splits always_stocked into (excluded, needed) based on low_stock flags, matching by
    normalized name so a low_stock entry like "toor dal" correctly matches an always_stocked
    entry like "Toor Dal (Togari Bele)" despite the exact strings differing."""
    always_stocked = pantry.get("always_stocked", [])
    low_stock = pantry.get("low_stock", [])
    low_stock_norms = {normalize_item_name(x) for x in low_stock}

    excluded, needed = [], []
    for entry in always_stocked:
        if normalize_item_name(entry) in low_stock_norms:
            needed.append(entry)
        else:
            excluded.append(entry)

    # A low_stock entry that isn't a recognized staple (e.g. a one-off "need onions this
    # week" note) still counts as needed, even though it has no always_stocked match.
    recognized_norms = {normalize_item_name(x) for x in always_stocked}
    for entry in low_stock:
        if normalize_item_name(entry) not in recognized_norms:
            needed.append(entry)

    return excluded, needed


def filter_pantry_matches(plan: dict, excluded: list) -> dict:
    """Safety net: even with an explicit prompt instruction, a 100+ item exclusion list
    sometimes gets partially ignored. This deterministically drops any grocery-list item
    that's an exact normalized match for a pantry item known to already be in stock."""
    excluded_norms = {normalize_item_name(x) for x in excluded}

    for category in plan.get("grocery_list", []):
        category["items"] = [
            item for item in category.get("items", [])
            if normalize_item_name(item.get("name", "")) not in excluded_norms
        ]

    plan["grocery_list"] = [c for c in plan.get("grocery_list", []) if c.get("items")]
    return plan
PANTRY_FILE = BASE_DIR / "pantry.json"


# ---------- 1. Load config ----------

def load_preferences() -> dict:
    with open(PREFERENCES_FILE, "r") as f:
        return json.load(f)


def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)


def load_pantry() -> dict:
    if not PANTRY_FILE.exists():
        return {"always_stocked": [], "low_stock": []}
    with open(PANTRY_FILE, "r") as f:
        return json.load(f)


def reset_pantry_low_stock(pantry: dict):
    """Clear the low_stock flags after a successful run — they've now been shopped for."""
    pantry["low_stock"] = []
    with open(PANTRY_FILE, "w") as f:
        json.dump(pantry, f, indent=2)


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

def build_prompt(preferences: dict, avoid_dishes: list, pantry: dict, week_start: date) -> str:
    meals = preferences.get("meals_per_day", ["breakfast", "lunch", "dinner"])
    day_names = [(week_start + timedelta(days=i)).strftime("%A") for i in range(7)]

    avoid_text = (
        "Avoid repeating these dishes from recent weeks: " + ", ".join(avoid_dishes)
        if avoid_dishes else "No recent history yet — any dishes are fine."
    )

    excluded, needed = resolve_pantry(pantry)

    pantry_text = (
        "The following are kept stocked in the pantry — do NOT include them in the "
        f"grocery list under any name/variant: {', '.join(sorted(excluded))}. Only skip an "
        "item if it's genuinely the same product — if a recipe needs a different form (e.g. "
        "whole vs. split, roasted vs. raw) than what's listed as stocked, still include it."
        if excluded else "No standing pantry staples are configured."
    )
    if needed:
        pantry_text += (
            " The following ARE currently needed even though some are normally staples — "
            f"make sure they appear on the grocery list: {', '.join(sorted(needed))}."
        )

    people = preferences.get("people", [])
    people_text = "\n".join(
        f"- {p.get('name')}: goal — {p.get('goals')}; can eat — {p.get('dietary_restrictions')}"
        for p in people
    ) or "No individual profiles configured."

    return f"""
You are a meal-planning and grocery-shopping assistant for a household of {len(people) or 2}.

Household members and their individual goals/restrictions:
{people_text}

Shared notes: {preferences.get('shared_health_notes')}

Plan meals that work for everyone eating together where possible. Since dietary
restrictions differ between household members, default to dishes compatible with the
MOST restrictive person's diet, and where a dish would normally include meat, treat the
meat as an optional add-on portion for whichever household member(s) can eat it (call
this out explicitly in that day's meal name, e.g. "Vegetable curry (+ grilled chicken for
Karthik)"), rather than planning entirely separate meals.

Cuisine mix: {preferences.get('cuisine_mix')}
{avoid_text}
{pantry_text}

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
    api_key = clean_secret(os.environ["GEMINI_API_KEY"])
    # Google periodically retires model versions. If this starts 404ing again, check
    # https://ai.google.dev/gemini-api/docs/models for the current free-tier Flash model
    # and either edit the default below or set a GEMINI_MODEL repo secret to override it
    # without touching code.
    model = clean_secret(os.environ.get("GEMINI_MODEL") or "") or "gemini-3.5-flash"
    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=model,
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
    gmail_address = clean_secret(os.environ["GMAIL_ADDRESS"])
    gmail_app_password = clean_secret(os.environ["GMAIL_APP_PASSWORD"])

    # RECIPIENT_EMAIL supports one address or several, comma-separated
    # (e.g. "karthik@gmail.com,raksha@gmail.com").
    raw_recipients = os.environ.get("RECIPIENT_EMAIL", gmail_address)
    recipients = [clean_secret(addr) for addr in raw_recipients.split(",") if addr.strip()]
    if not recipients:
        recipients = [gmail_address]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, recipients, msg.as_string())


def send_whatsapp(body: str):
    """Optional. Uses CallMeBot's free personal WhatsApp API.
    Skipped automatically if the secrets aren't set — see README for setup.
    """
    phone = os.environ.get("CALLMEBOT_PHONE")
    api_key = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not api_key:
        print("CallMeBot credentials not set — skipping WhatsApp send.")
        return
    phone = clean_secret(phone)
    api_key = clean_secret(api_key)

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
    pantry = load_pantry()
    avoid = recent_dishes(history, preferences.get("weeks_of_history_to_avoid_repeating", 3))

    # Shopping happens the day after this runs (Saturday run -> Sunday start).
    week_start = date.today() + timedelta(days=1)

    prompt = build_prompt(preferences, avoid, pantry, week_start)
    plan = call_gemini(prompt)

    excluded, _ = resolve_pantry(pantry)
    plan = filter_pantry_matches(plan, excluded)

    email_body = format_email_body(plan)
    send_email(subject=f"Grocery list — week of {plan.get('week_start')}", body=email_body)
    print("Email sent.")

    send_whatsapp(format_whatsapp_body(plan))

    keep_weeks = max(preferences.get("weeks_of_history_to_avoid_repeating", 3), 1) + 1
    update_history(history, plan, keep_weeks)
    print("History updated.")

    # Only clear the low_stock flags once everything above succeeded.
    reset_pantry_low_stock(pantry)
    print("Pantry low_stock flags reset.")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        print(f"Missing required environment variable/secret: {e}", file=sys.stderr)
        sys.exit(1)
