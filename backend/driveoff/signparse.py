"""Turn a plain-English parking sign into a structured RateRule (Gemini), with a deterministic fallback.

The operator always reviews the parsed rule (shown back in plain English) and confirms it before it goes live.
"""
from __future__ import annotations

import re

from . import gemini
from .rates import FreeWindow, RateRule

PROMPT = """You convert a parking lot's posted rate sign into a billing rule. Be literal: never invent fees,
surcharges or surge pricing that the sign does not state. If something is ambiguous, list it in "questions".

Sign:
\"\"\"{sign}\"\"\"

Fields:
- rate_per_hour_cents (int): price per hour in cents
- free_minutes (int): free minutes at the start of every stay (0 if none)
- grace_minutes (int): if the entire stay is this short it's free, e.g. "no charge under 10 min" (0 if none)
- increment_minutes (int): billing increment; "per hour" or "/hr" = 60, "per 15 min" = 15, default 60
- daily_cap_cents (int or null): max per calendar day
- free_windows (list): periods that are always free, each {{"days": [0-6, Monday=0], "start": "HH:MM", "end": "HH:MM"}};
  a whole free day uses start "00:00" and end "00:00"; overnight windows wrap (start "18:00", end "08:00")
- questions (list of strings): anything the operator should clarify
"""

SCHEMA = ('{"rate_per_hour_cents": int, "free_minutes": int, "grace_minutes": int, "increment_minutes": int, '
          '"daily_cap_cents": int|null, "free_windows": [{"days": [int], "start": "HH:MM", "end": "HH:MM"}], '
          '"questions": [str]}')

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _money(s: str) -> int:
    return int(round(float(s) * 100))


def fallback_parse(sign: str) -> dict:
    """Regex parser for common phrasings; used when Gemini is unavailable. Deliberately conservative."""
    s = sign.lower()
    out = {"rate_per_hour_cents": 0, "free_minutes": 0, "grace_minutes": 0, "increment_minutes": 60,
           "daily_cap_cents": None, "free_windows": [], "questions": []}
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*(?:/|per)\s*(hr|hour|h)\b", s)
    if m:
        out["rate_per_hour_cents"] = _money(m.group(1))
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*(?:/|per|every)\s*(\d+)\s*min", s)
    if m:
        per = int(m.group(2))
        out["rate_per_hour_cents"] = _money(m.group(1)) * 60 // per
        out["increment_minutes"] = per
    m = re.search(r"first\s+(\d+)\s*(min|minutes|hr|hour|hours)\s+free", s)
    if m:
        out["free_minutes"] = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
    m = re.search(r"(?:max|maximum|cap|daily max)\s*\$\s*(\d+(?:\.\d+)?)", s)
    if m:
        out["daily_cap_cents"] = _money(m.group(1))
    m = re.search(r"(?:under|less than|grace)\s*(\d+)\s*min", s)
    if m:
        out["grace_minutes"] = int(m.group(1))
    for d, i in DAYS.items():
        if re.search(rf"free\s+(?:all day\s+|on\s+)?{d}\w*", s) or re.search(rf"{d}\w*\s+free", s) or \
                re.search(rf"free[^.]*\b(?:all day|on)\s+{d}\w*", s):
            out["free_windows"].append({"days": [i], "start": "00:00", "end": "00:00"})

    def hhmm(h, ap):
        h = int(h) % 12 + (12 if ap == "pm" else 0)
        return f"{h:02d}:00"
    m = re.search(r"free\s+(\d{1,2})\s*(am|pm)\s*(?:-|to|–)\s*(\d{1,2})\s*(am|pm)", s)
    if m:
        out["free_windows"].append({"days": list(range(7)), "start": hhmm(m.group(1), m.group(2)),
                                    "end": hhmm(m.group(3), m.group(4))})
    if not out["rate_per_hour_cents"]:
        out["questions"].append("Couldn't find an hourly rate on the sign.")
    return out


def parse_sign(sign: str, timezone: str = "America/Los_Angeles") -> dict:
    source = "gemini"
    try:
        raw = gemini.generate_json(PROMPT.format(sign=sign), schema_hint=SCHEMA)
    except gemini.GeminiUnavailable as e:
        raw, source = fallback_parse(sign), f"fallback-regex ({e})"
    questions = raw.pop("questions", []) or []
    meta = {k: raw.pop(k) for k in list(raw) if k.startswith("_")}
    raw["free_windows"] = [FreeWindow(**w) for w in raw.get("free_windows") or []]
    raw.setdefault("increment_minutes", 60)
    rule = RateRule.from_dict({**raw, "timezone": timezone, "sign_text": sign})
    errs = rule.validate()
    return {"rule": rule.to_dict(), "plain_english": rule.describe(), "questions": questions, "errors": errs,
            "source": source, "meta": meta}
