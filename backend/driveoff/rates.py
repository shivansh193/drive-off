"""Posted-rate engine. Pure, deterministic, integer cents. No surge, no hidden fees.

A stay is billed minute by minute in the lot's local time:
  1. If the whole stay is within `grace_minutes`, it is free (drive-through / changed-my-mind).
  2. Minutes inside a free window (e.g. Sundays, 18:00-08:00) are never billed.
  3. The first `free_minutes` billable minutes of the stay are free.
  4. Remaining billable minutes are grouped by local calendar day (so midnight rolls over to a new day),
     rounded up to `increment_minutes`, priced at `rate_per_hour_cents`, and capped at `daily_cap_cents` per day.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo


@dataclass
class FreeWindow:
    days: list[int] = field(default_factory=lambda: list(range(7)))  # 0 = Monday
    start: str = "00:00"
    end: str = "00:00"  # end <= start means the window wraps past midnight; equal means all day

    def contains(self, t: datetime) -> bool:
        s = time.fromisoformat(self.start)
        e = time.fromisoformat(self.end)
        tt = t.time()
        if s == e:
            return t.weekday() in self.days
        if s < e:
            return t.weekday() in self.days and s <= tt < e
        # wraps midnight: the part after midnight belongs to the previous day's window
        if tt >= s:
            return t.weekday() in self.days
        return (t.weekday() - 1) % 7 in self.days and tt < e


@dataclass
class RateRule:
    rate_per_hour_cents: int
    free_minutes: int = 0
    grace_minutes: int = 0
    increment_minutes: int = 60
    daily_cap_cents: int | None = None
    free_windows: list[FreeWindow] = field(default_factory=list)
    timezone: str = "America/Los_Angeles"
    currency: str = "USD"
    sign_text: str = ""

    @staticmethod
    def from_dict(d: dict) -> "RateRule":
        d = dict(d)
        d["free_windows"] = [w if isinstance(w, FreeWindow) else FreeWindow(**w) for w in d.get("free_windows", [])]
        allowed = set(RateRule.__dataclass_fields__)
        return RateRule(**{k: v for k, v in d.items() if k in allowed})

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> list[str]:
        errs = []
        if self.rate_per_hour_cents < 0:
            errs.append("rate must be >= 0")
        if not 1 <= self.increment_minutes <= 1440:
            errs.append("increment must be 1-1440 minutes")
        if self.free_minutes < 0 or self.grace_minutes < 0:
            errs.append("free and grace minutes must be >= 0")
        if self.daily_cap_cents is not None and self.daily_cap_cents < 0:
            errs.append("daily cap must be >= 0")
        try:
            ZoneInfo(self.timezone)
        except Exception:
            errs.append(f"unknown timezone {self.timezone}")
        return errs

    def describe(self) -> str:
        parts = []
        if self.free_minutes:
            parts.append(f"first {self.free_minutes} min free")
        parts.append(f"${self.rate_per_hour_cents / 100:.2f}/hr billed per {self.increment_minutes} min")
        if self.daily_cap_cents is not None:
            parts.append(f"max ${self.daily_cap_cents / 100:.2f}/day")
        if self.grace_minutes:
            parts.append(f"{self.grace_minutes} min grace")
        for w in self.free_windows:
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            days = "daily" if len(w.days) == 7 else ",".join(names[d] for d in sorted(w.days))
            parts.append(f"free {w.start}-{w.end} ({days})")
        return "; ".join(parts)


@dataclass
class Quote:
    amount_cents: int
    minutes_total: int
    minutes_billable: int
    days: list[dict]
    explanation: str

    def to_dict(self) -> dict:
        return asdict(self)


def price(rule: RateRule, entry: datetime, exit: datetime) -> Quote:
    if entry.tzinfo is None or exit.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    if exit < entry:
        raise ValueError("exit before entry")
    tz = ZoneInfo(rule.timezone)
    start = entry.astimezone(tz)
    total = int((exit - entry).total_seconds() // 60)
    if total <= rule.grace_minutes:
        return Quote(0, total, 0, [], f"{total} min stay is within the {rule.grace_minutes} min grace period")

    # Walk the billable minutes in order. Every `increment_minutes` of billable time is one block, and each block
    # (including a final partial block) is counted once, on the calendar day it starts. So a stay that crosses
    # midnight is never rounded up twice; the daily cap is still applied per calendar day.
    per_day: "OrderedDict[str, int]" = OrderedDict()
    blocks_per_day: "OrderedDict[str, int]" = OrderedDict()
    free_left = rule.free_minutes
    n = 0
    for i in range(total):
        t = start + timedelta(minutes=i)
        if any(w.contains(t) for w in rule.free_windows):
            continue
        if free_left > 0:
            free_left -= 1
            continue
        day = t.date().isoformat()
        per_day[day] = per_day.get(day, 0) + 1
        if n % rule.increment_minutes == 0:
            blocks_per_day[day] = blocks_per_day.get(day, 0) + 1
        blocks_per_day.setdefault(day, 0)
        n += 1

    days, amount = [], 0
    for day, mins in per_day.items():
        blocks = blocks_per_day.get(day, 0)
        raw = (blocks * rule.increment_minutes * rule.rate_per_hour_cents + 59) // 60  # round half-cent up
        capped = raw if rule.daily_cap_cents is None else min(raw, rule.daily_cap_cents)
        days.append({"date": day, "billable_minutes": mins, "raw_cents": raw, "charged_cents": capped,
                     "capped": capped < raw})
        amount += capped
    billable = sum(per_day.values())
    expl = f"{total} min stay, {billable} billable after free time; " + ", ".join(
        f"{d['date']}: ${d['charged_cents'] / 100:.2f}{' (daily cap)' if d['capped'] else ''}" for d in days)
    return Quote(amount, total, billable, days, expl)
