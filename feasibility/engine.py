"""Candidate implementation goes here.

Implement ``evaluate_offer`` so that it satisfies the rules in ASSIGNMENT.md and
the example expectations in tests/test_cases.py. The dataclasses below define the
required OUTPUT shape (see ASSIGNMENT.md "Output"). You may add helpers, modules,
or rewrite internals freely, but keep ``evaluate_offer``'s signature and the
serialized shape of ``Result`` (so the runner and tests work).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Set, Tuple

from feasibility.models import Client, CreditorRules, Offer

# Modules added as part of our implementation (date_utils, scheduler, simulator, floors
# are all new files we wrote; floors is used transitively via scheduler).
from .date_utils import generate_cadence_dates, get_first_payment_date, round_half_up
from .scheduler import best_k_balloon, best_k_even, best_k_staircase
from .simulator import ScheduleEntry, simulate


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    # One of "even", "staircase", or "balloon" — the shape your solution produced
    # (driven by the creditor flags). None when infeasible.
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


# ===========================================================================
# Everything below this line is our implementation of evaluate_offer.
# The output dataclasses above (ScheduleRow, FundsOption, AdditionalFunds,
# Result) were pre-provided by the assignment and are left unchanged.
# ===========================================================================

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _future_ledger(client: Client):
    return [e for e in client.ledger if e.date > client.as_of_date]


def _draft_dates(client: Client) -> Set[date]:
    return {e.date for e in client.ledger if e.type == "credit" and e.date > client.as_of_date}


def _fee_score(entries: List[ScheduleEntry]) -> int:
    """Lower = more front-loaded. Weighted sum: fee_cents * position_index."""
    return sum(e.program_fee_cents * i for i, e in enumerate(entries))


def _to_schedule_rows(entries: List[ScheduleEntry]) -> List[ScheduleRow]:
    return [
        ScheduleRow(
            date=e.date,
            creditor_payment_cents=e.creditor_payment_cents,
            program_fee_cents=e.program_fee_cents,
            bank_fee_cents=e.bank_fee_cents,
            balance_cents=e.balance_cents,
        )
        for e in entries
    ]


def _try_candidates(
    candidates: List[Tuple[int, List[int]]],
    client: Client,
    cadence_dates: List[date],
    first_payment_date: date,
    bank_fee_cents: int,
    program_fee_total: int,
    extra_credit_date: Optional[date] = None,
    extra_credit_amount: int = 0,
    draft_increment: int = 0,
) -> Optional[List[ScheduleEntry]]:
    future = _future_ledger(client)
    draft_dates = _draft_dates(client)
    best_entries = None
    best_score = None

    for k, payments in candidates:
        feasible, entries = simulate(
            initial_balance=client.current_balance_cents,
            future_ledger=future,
            cadence_dates=cadence_dates,
            payment_dates=cadence_dates[:k],
            payments=payments,
            bank_fee_cents=bank_fee_cents,
            program_fee_total=program_fee_total,
            first_payment_date=first_payment_date,
            extra_credit_date=extra_credit_date,
            extra_credit_amount=extra_credit_amount,
            draft_increment=draft_increment,
            draft_dates=draft_dates,
        )
        if not feasible:
            continue
        score = _fee_score(entries)
        if best_score is None or score < best_score:
            best_score = score
            best_entries = entries

    return best_entries


def _try_generate(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credit_date: Optional[date] = None,
    extra_credit_amount: int = 0,
    draft_increment: int = 0,
) -> Tuple[bool, Optional[List[ScheduleEntry]], Optional[str]]:
    """
    Attempt to build a feasible schedule.

    Uses round_half_up (not Python's built-in round) to match the spec's
    rounding requirement. offer.creditor_balance_cents is the creditor balance.

    Returns (feasible, schedule_entries, shape_name).
    """
    offer_total = round_half_up(offer.settlement_pct * offer.creditor_balance_cents)
    program_fee = round_half_up(rules.program_fee_pct * offer.original_balance_cents)
    first_payment_date = get_first_payment_date(client.first_draft_date, offer.first_payment_date)
    horizon = client.last_draft_date

    if first_payment_date > horizon:
        return False, None, None

    # All monthly cadence dates from first_payment_date up to (and including) the horizon.
    # max_installments is capped by both the creditor rules and how many dates actually fit.
    cadence_dates = generate_cadence_dates(first_payment_date, horizon)
    max_installments = min(rules.max_payments, rules.max_terms, len(cadence_dates))

    common = dict(
        client=client,
        cadence_dates=cadence_dates,
        first_payment_date=first_payment_date,
        bank_fee_cents=rules.bank_fee_cents,
        program_fee_total=program_fee,
        extra_credit_date=extra_credit_date,
        extra_credit_amount=extra_credit_amount,
        draft_increment=draft_increment,
    )

    # Shape is determined solely by the creditor flags (§6 of the assignment).
    # Candidates are tried in reverse-k order so larger k (more installments, smaller early payments) is preferred when fee scores are equal.
    if rules.even_pays:
        entries = _try_candidates(list(reversed(best_k_even(max_installments, offer_total, rules))), **common)
        if entries is not None:
            return True, entries, "even"
    elif rules.is_ballooning_allowed:
        entries = _try_candidates(list(reversed(best_k_balloon(max_installments, offer_total, rules))), **common)
        if entries is not None:
            return True, entries, "balloon"
    else:
        entries = _try_candidates(list(reversed(best_k_staircase(max_installments, offer_total, rules))), **common)
        if entries is not None:
            return True, entries, "staircase"

    return False, None, None


# ---------------------------------------------------------------------------
# Part 2 — minimum extra funding
# ---------------------------------------------------------------------------

def _find_min_lump_sum(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    """
    Binary-search for the smallest one-time credit L that makes a feasible schedule exist.

    Lump is placed on the earliest future ledger date (an earlier credit is always
    weakly more useful than a later one of the same size).

    Upper bound = total outgoing (offer + fee + all bank fees). We do NOT divide by
    the number of drafts because some drafts may arrive after the last cadence date
    and cannot contribute to any obligation — dividing would underestimate the bound.
    """
    future = _future_ledger(client)
    if future:
        lump_date = min(e.date for e in future)
    else:
        lump_date = get_first_payment_date(client.first_draft_date, offer.first_payment_date)

    offer_total = round_half_up(offer.settlement_pct * offer.creditor_balance_cents)
    program_fee = round_half_up(rules.program_fee_pct * offer.original_balance_cents)
    max_installments = min(rules.max_payments, rules.max_terms)
    upper = offer_total + program_fee + max_installments * rules.bank_fee_cents + 1

    lo, hi = 0, upper
    while lo < hi:
        mid = (lo + hi) // 2
        feasible, _, _ = _try_generate(client, offer, rules, extra_credit_date=lump_date, extra_credit_amount=mid)
        if feasible:
            hi = mid
        else:
            lo = mid + 1

    L = lo
    max_allowed = round_half_up(0.65 * offer_total)
    within = L <= max_allowed
    reason = "" if within else f"Lump sum {L} exceeds guardrail of {max_allowed} (65% of offer_total)"
    return FundsOption(amount_cents=L, within_guardrail=within, reason=reason, date=lump_date)


def _find_min_monthly_increment(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    """
    Binary-search for the smallest uniform increment X added to every future draft
    that makes a feasible schedule exist.

    Same upper bound rationale as _find_min_lump_sum — do not divide by draft count.
    """
    draft_dates = _draft_dates(client)
    N = len(draft_dates)  # reported as num_drafts in the output

    offer_total = round_half_up(offer.settlement_pct * offer.creditor_balance_cents)
    program_fee = round_half_up(rules.program_fee_pct * offer.original_balance_cents)
    max_installments = min(rules.max_payments, rules.max_terms)
    upper = offer_total + program_fee + max_installments * rules.bank_fee_cents + 1

    lo, hi = 0, upper
    while lo < hi:
        mid = (lo + hi) // 2
        feasible, _, _ = _try_generate(client, offer, rules, draft_increment=mid)
        if feasible:
            hi = mid
        else:
            lo = mid + 1

    X = lo
    guardrail = max(10000, round_half_up(0.40 * client.draft_amount_cents))
    within = X <= guardrail
    reason = "" if within else f"Monthly increment {X} exceeds guardrail of {guardrail}"
    return FundsOption(amount_cents=X, within_guardrail=within, reason=reason, num_drafts=N)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification.

    Return a Result with feasible=True and a schedule when the offer fits, or
    feasible=False with additional_funds (minimum lump sum AND minimum monthly
    increment) when it does not.
    """
    feasible, entries, shape = _try_generate(client, offer, rules)

    if feasible:
        return Result(
            feasible=True,
            pay_shape_used=shape,
            schedule=_to_schedule_rows(entries),
        )

    lump = _find_min_lump_sum(client, offer, rules)
    increment = _find_min_monthly_increment(client, offer, rules)
    return Result(
        feasible=False,
        additional_funds=AdditionalFunds(lump_sum=lump, monthly_increment=increment),
    )
