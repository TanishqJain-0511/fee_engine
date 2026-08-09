"""
Date and rounding utilities for the fee engine.

Provides:
- round_half_up: spec-required rounding (0.5 always rounds away from zero)
- generate_cadence_dates: monthly payment dates from a start date up to the horizon
- get_first_payment_date: resolves the effective first payment date from the offer or defaults to EOM
- advance_month: single-month step with EOM-snapping or day-clamping
"""
import calendar
import math
from datetime import date
from typing import List


def round_half_up(n: float) -> int:
    """Round-half-up: 0.5 always rounds away from zero (positive)."""
    return math.floor(n + 0.5)


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def end_of_month(d: date) -> date:
    return d.replace(day=last_day_of_month(d.year, d.month))


def is_end_of_month(d: date) -> bool:
    return d.day == last_day_of_month(d.year, d.month)


def advance_month(d: date, is_eom: bool, target_day: int) -> date:
    """Advance one month. If is_eom, snap to end-of-month; otherwise clamp target_day
    to the new month's length.

    target_day is fixed for the whole cadence (the day-of-month of first_payment_date),
    so a short month clamping it down doesn't affect later, longer months.
    """
    month = d.month + 1
    year = d.year
    if month > 12:
        month = 1
        year += 1
    if is_eom:
        day = last_day_of_month(year, month)
    else:
        day = min(target_day, last_day_of_month(year, month))
    return date(year, month, day)


def generate_cadence_dates(first_payment_date: date, horizon: date) -> List[date]:
    """
    Generate all monthly cadence dates from first_payment_date up to and including horizon.

    If first_payment_date is the last day of its month, every subsequent date
    snaps to the last day of each month (true end-of-month). Otherwise the
    same day-of-month is used, clamped to the month length.
    """
    dates: List[date] = []
    eom = is_end_of_month(first_payment_date)
    target_day = first_payment_date.day
    current = first_payment_date
    while current <= horizon:
        dates.append(current)
        current = advance_month(current, eom, target_day)
    return dates


def get_first_payment_date(client_first_draft_date: date, offer_first_payment_date) -> date:
    """Return the effective first payment date."""
    if offer_first_payment_date is not None:
        return offer_first_payment_date
    return end_of_month(client_first_draft_date)
