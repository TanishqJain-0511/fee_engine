"""
Ledger simulation: given a payment schedule (creditor payments on specific dates),
greedily assign program fee (front-loaded) and verify balance ≥ 0 at every date.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from .models import LedgerEntry


# Internal result type for simulate(). engine.py converts these to ScheduleRow
# (the public output class defined there) to keep this module free of circular imports.
@dataclass
class ScheduleEntry:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


def simulate(
    initial_balance: int,
    future_ledger: List[LedgerEntry],          # future committed entries (credits & debits)
    cadence_dates: List[date],                 # all cadence dates first_payment_date..horizon
    payment_dates: List[date],                 # cadence dates that carry creditor payments
    payments: List[int],                       # creditor payment amounts (same length)
    bank_fee_cents: int,
    program_fee_total: int,
    first_payment_date: date,
    extra_credit_date: Optional[date] = None,
    extra_credit_amount: int = 0,
    draft_increment: int = 0,
    draft_dates: Optional[Set[date]] = None,   # future draft dates (for increment)
) -> Tuple[bool, Optional[List[ScheduleEntry]]]:
    """
    Simulate the ledger and return (feasible, schedule_entries).

    On each date:
      1. Apply all credits (credits before debits rule)
      2. Apply committed debits
      3. Apply creditor payment + bank fee (check balance ≥ 0)
      4. If cadence date ≥ first_payment_date: greedily collect program fee
         (collect as much as possible without going negative)

    Returns (False, None) if balance ever goes negative or fee not fully collected.
    """
    if draft_dates is None:
        draft_dates = set()

    # Build per-date aggregates
    date_credits: Dict[date, int] = defaultdict(int)
    date_committed_debits: Dict[date, int] = defaultdict(int)

    for entry in future_ledger:
        if entry.type == "credit":
            amt = entry.amount_cents
            if entry.date in draft_dates:
                amt += draft_increment
            date_credits[entry.date] += amt
        else:
            date_committed_debits[entry.date] += entry.amount_cents

    if extra_credit_amount > 0 and extra_credit_date is not None:
        date_credits[extra_credit_date] += extra_credit_amount

    # Creditor payments & bank fees by date
    date_creditor: Dict[date, int] = defaultdict(int)
    date_bank: Dict[date, int] = defaultdict(int)
    payment_date_set: Set[date] = set(payment_dates)
    for d, p in zip(payment_dates, payments):
        date_creditor[d] += p
        if bank_fee_cents > 0:
            date_bank[d] += bank_fee_cents

    # All dates we need to process
    all_dates: Set[date] = (
        set(date_credits.keys())
        | set(date_committed_debits.keys())
        | payment_date_set
        | set(cadence_dates)
    )

    cadence_set: Set[date] = set(cadence_dates)
    sorted_dates = sorted(all_dates)

    balance = initial_balance
    fee_remaining = program_fee_total
    schedule_entries: List[ScheduleEntry] = []

    for d in sorted_dates:
        # 1. Credits
        balance += date_credits[d]

        # 2. Committed debits
        balance -= date_committed_debits[d]
        if balance < 0:
            return False, None

        # 3. Creditor payment + bank fee
        creditor_pmt = date_creditor[d]
        bank_fee = date_bank[d]
        balance -= creditor_pmt + bank_fee
        if balance < 0:
            return False, None

        # 4. Program fee (greedy, only on cadence dates ≥ first_payment_date)
        fee_collected = 0
        if d in cadence_set and d >= first_payment_date and fee_remaining > 0:
            fee_collected = min(fee_remaining, balance)
            balance -= fee_collected
            fee_remaining -= fee_collected

        # Record schedule entry if this is a cadence date with any activity
        if d in cadence_set and (creditor_pmt > 0 or fee_collected > 0 or bank_fee > 0):
            schedule_entries.append(
                ScheduleEntry(
                    date=d,
                    creditor_payment_cents=creditor_pmt,
                    program_fee_cents=fee_collected,
                    bank_fee_cents=bank_fee,
                    balance_cents=balance,
                )
            )

    if fee_remaining > 0:
        return False, None

    return True, schedule_entries
