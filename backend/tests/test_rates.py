"""Property tests for the posted-rate engine: caps, grace, free time, midnight rollover, monotonicity."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hypothesis import given, settings, strategies as st

from driveoff.rates import FreeWindow, RateRule, price

LA = ZoneInfo("America/Los_Angeles")
BASE = datetime(2026, 11, 10, 9, 0, tzinfo=LA)

rules = st.builds(
    RateRule,
    rate_per_hour_cents=st.integers(0, 2000),
    free_minutes=st.integers(0, 120),
    grace_minutes=st.integers(0, 20),
    increment_minutes=st.sampled_from([1, 5, 15, 30, 60]),
    daily_cap_cents=st.one_of(st.none(), st.integers(0, 6000)),
)
starts = st.integers(0, 60 * 24 * 7).map(lambda m: BASE + timedelta(minutes=m))
durations = st.integers(0, 60 * 24 * 3)


def demo_rule():
    return RateRule(rate_per_hour_cents=300, free_minutes=30, daily_cap_cents=2000)


def test_examples_from_the_sign():
    r = demo_rule()  # First 30 min free, then $3/hr, max $20/day
    assert price(r, BASE, BASE + timedelta(minutes=25)).amount_cents == 0
    assert price(r, BASE, BASE + timedelta(minutes=31)).amount_cents == 300
    assert price(r, BASE, BASE + timedelta(minutes=90)).amount_cents == 300
    assert price(r, BASE, BASE + timedelta(minutes=91)).amount_cents == 600
    assert price(r, BASE, BASE + timedelta(hours=10)).amount_cents == 2000  # capped


def test_midnight_rollover_starts_a_new_day_cap():
    r = demo_rule()
    entry = datetime(2026, 11, 10, 18, 0, tzinfo=LA)
    q = price(r, entry, entry + timedelta(hours=16))  # 18:00 -> 10:00 next day
    assert [d["date"] for d in q.days] == ["2026-11-10", "2026-11-11"]
    # day 1: 6h minus 30 free = 5.5h -> 6 blocks = $18 ; day 2: 10h = $30 -> capped $20
    assert q.days[0]["charged_cents"] == 1800 and q.days[1]["charged_cents"] == 2000
    assert q.amount_cents == 3800


def test_grace_period_is_free_even_with_no_free_minutes():
    r = RateRule(rate_per_hour_cents=500, grace_minutes=10)
    assert price(r, BASE, BASE + timedelta(minutes=10)).amount_cents == 0
    assert price(r, BASE, BASE + timedelta(minutes=11)).amount_cents == 500


def test_free_window_overnight_and_sundays():
    r = RateRule(rate_per_hour_cents=600, free_windows=[FreeWindow(start="18:00", end="08:00"),
                                                        FreeWindow(days=[6], start="00:00", end="00:00")])
    evening = datetime(2026, 11, 10, 19, 0, tzinfo=LA)
    assert price(r, evening, evening + timedelta(hours=12)).amount_cents == 0  # 19:00-07:00 all free
    sunday = datetime(2026, 11, 15, 10, 0, tzinfo=LA)
    assert price(r, sunday, sunday + timedelta(hours=5)).amount_cents == 0


def test_dst_fall_back_day_is_priced_on_real_minutes():
    r = RateRule(rate_per_hour_cents=100, increment_minutes=1)
    entry = datetime(2026, 11, 1, 0, 30, tzinfo=LA)  # DST ends 02:00 -> 01:00
    exit_ = entry + timedelta(hours=3)
    assert price(r, entry, exit_).minutes_total == 180


@settings(max_examples=300, deadline=None)
@given(rules, starts, durations)
def test_never_exceeds_cap_per_day(rule, start, minutes):
    q = price(rule, start, start + timedelta(minutes=minutes))
    if rule.daily_cap_cents is not None:
        assert all(d["charged_cents"] <= rule.daily_cap_cents for d in q.days)
    assert q.amount_cents == sum(d["charged_cents"] for d in q.days)
    assert q.amount_cents >= 0


@settings(max_examples=300, deadline=None)
@given(rules, starts, durations, st.integers(1, 600))
def test_staying_longer_never_costs_less(rule, start, minutes, extra):
    a = price(rule, start, start + timedelta(minutes=minutes)).amount_cents
    b = price(rule, start, start + timedelta(minutes=minutes + extra)).amount_cents
    if minutes > rule.grace_minutes:
        assert b >= a


@settings(max_examples=200, deadline=None)
@given(rules, starts, durations)
def test_never_more_than_posted_hourly_rate_rounded_up(rule, start, minutes):
    """No hidden fees: the charge never exceeds billable time rounded up to the increment at the posted rate."""
    q = price(rule, start, start + timedelta(minutes=minutes))
    bound = sum(-(-d["billable_minutes"] // rule.increment_minutes) * rule.increment_minutes
                * rule.rate_per_hour_cents / 60 + 1 for d in q.days)
    assert q.amount_cents <= bound


@settings(max_examples=200, deadline=None)
@given(rules, starts, st.integers(0, 20))
def test_within_grace_or_free_is_zero(rule, start, minutes):
    if minutes <= max(rule.grace_minutes, rule.free_minutes):
        assert price(rule, start, start + timedelta(minutes=minutes)).amount_cents == 0


def test_timezone_aware_required():
    import pytest
    with pytest.raises(ValueError):
        price(demo_rule(), datetime(2026, 1, 1), datetime(2026, 1, 1, 2))
    with pytest.raises(ValueError):
        price(demo_rule(), BASE, BASE - timedelta(minutes=1))


def test_round_trip_and_utc_inputs():
    r = demo_rule()
    r2 = RateRule.from_dict(r.to_dict())
    assert r2 == r
    utc_entry = BASE.astimezone(timezone.utc)
    assert price(r, utc_entry, utc_entry + timedelta(hours=2)).amount_cents == price(
        r, BASE, BASE + timedelta(hours=2)).amount_cents


def test_sign_fallback_parser_reads_free_windows_and_grace():
    from driveoff.signparse import fallback_parse
    r = fallback_parse("$2.50 per 30 min, max $18 per day. Free 6pm-8am and all day Sunday. No charge under 10 minutes.")
    assert r["rate_per_hour_cents"] == 500 and r["increment_minutes"] == 30 and r["daily_cap_cents"] == 1800
    assert r["grace_minutes"] == 10
    assert {"days": [6], "start": "00:00", "end": "00:00"} in r["free_windows"]
    assert any(w["start"] == "18:00" and w["end"] == "08:00" for w in r["free_windows"])
    r2 = fallback_parse("First 30 min free, then $3/hr, max $20/day")
    assert (r2["free_minutes"], r2["rate_per_hour_cents"], r2["daily_cap_cents"]) == (30, 300, 2000)
