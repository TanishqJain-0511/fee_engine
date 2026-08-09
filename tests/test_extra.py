"""Additional coverage beyond the four provided cases in test_cases.py.

Targets the specific bullet list from ASSIGNMENT.md §10 "Deliverables":
token-pay and tier floors, the max_segments cap, exact-sum, the date-by-date
simulation (same-day ordering and a balance that hits exactly $0), the
horizon limit, fee compliance (no fee before the first payment), and both
Part 2 minima (including a guardrail-rejection case, which the provided
case2 doesn't exercise).
"""

from __future__ import annotations

from datetime import date

from feasibility.date_utils import round_half_up
from feasibility.engine import evaluate_offer
from feasibility.floors import compute_floors, validate_payments
from feasibility.models import Client, CreditorRules, LedgerEntry, Offer, load_case
from feasibility.scheduler import best_k_staircase
from feasibility.simulator import simulate


def _rules(**overrides) -> CreditorRules:
    base = dict(
        max_terms=12,
        max_payments=12,
        min_payment_cents=2500,
        max_token_pays=6,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=2,
        bank_fee_cents=500,
        program_fee_pct=0.2,
    )
    base.update(overrides)
    return CreditorRules(**base)


# ---------------------------------------------------------------------------
# Floors: token-pay budget and tier step-ups
# ---------------------------------------------------------------------------

def test_token_pay_budget_exhausts_then_bumps_floor():
    # max_token_pays=2 -> positions 1-2 may sit at the base min; position 3+ must exceed it.
    rules = _rules(min_payment_cents=2500, max_token_pays=2, min_payment_tiers=[])
    floors = compute_floors(5, rules)
    assert floors == [2500, 2500, 2501, 2501, 2501]


def test_tier_floor_overrides_token_pay_budget():
    # A tier that kicks in above the base min is not a "token pay" position at all,
    # even if token-pay budget remains.
    rules = _rules(min_payment_cents=2500, max_token_pays=6, min_payment_tiers=[(3, 5000)])
    floors = compute_floors(4, rules)
    assert floors == [2500, 2500, 5000, 5000]


def test_validate_payments_rejects_below_floor_and_non_monotonic():
    rules = _rules(min_payment_cents=2500, max_token_pays=1, min_payment_tiers=[])
    assert validate_payments([2500, 2600, 2700], rules, offer_total=7800) is True
    # Second token pay at base min exceeds max_token_pays=1.
    assert validate_payments([2500, 2500, 2800], rules, offer_total=7800) is False
    # Non-decreasing violated.
    assert validate_payments([2600, 2500], rules, offer_total=5100) is False
    # Wrong sum.
    assert validate_payments([2500, 2600], rules, offer_total=9999) is False


# ---------------------------------------------------------------------------
# max_segments cap
# ---------------------------------------------------------------------------

def test_max_segments_one_only_allows_flat_staircase():
    rules = _rules(min_payment_cents=1000, max_token_pays=10, min_payment_tiers=[], max_segments=1)
    for k, payments in best_k_staircase(max_installments=5, offer_total=5000, rules=rules):
        assert len(set(payments)) <= 1, payments


def test_max_segments_two_never_exceeds_two_distinct_levels():
    rules = _rules(min_payment_cents=1000, max_token_pays=10, min_payment_tiers=[], max_segments=2)
    for k, payments in best_k_staircase(max_installments=6, offer_total=7777, rules=rules):
        assert len(set(payments)) <= 2, payments


# ---------------------------------------------------------------------------
# Exact-sum constraint on real schedules
# ---------------------------------------------------------------------------

def test_case1_schedule_sums_exactly_to_offer_total():
    client, offer, rules = load_case("cases/case1_feasible_even")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    offer_total = round_half_up(offer.settlement_pct * offer.creditor_balance_cents)
    assert sum(row.creditor_payment_cents for row in r.schedule) == offer_total


def test_case4_schedule_sums_exactly_to_offer_total():
    client, offer, rules = load_case("cases/case4_tiers")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    offer_total = round_half_up(offer.settlement_pct * offer.creditor_balance_cents)
    assert sum(row.creditor_payment_cents for row in r.schedule) == offer_total


# ---------------------------------------------------------------------------
# Date-by-date simulation: same-day ordering, exact-zero balance
# ---------------------------------------------------------------------------

def test_same_day_credit_applies_before_debit():
    # A same-day credit and debit of equal size: if the debit were applied first
    # (wrong order), balance would dip negative and the simulation would fail.
    d = date(2026, 1, 1)
    future_ledger = [
        LedgerEntry(date=d, amount_cents=1000, type="credit"),
        LedgerEntry(date=d, amount_cents=1000, type="debit"),
    ]
    feasible, entries = simulate(
        initial_balance=0,
        future_ledger=future_ledger,
        cadence_dates=[],
        payment_dates=[],
        payments=[],
        bank_fee_cents=0,
        program_fee_total=0,
        first_payment_date=d,
    )
    assert feasible is True


def test_debit_first_would_fail_if_misordered():
    # Sanity check on the test above: with only a debit (no same-day credit to
    # offset it), the same balance goes negative and simulation must fail.
    d = date(2026, 1, 1)
    future_ledger = [LedgerEntry(date=d, amount_cents=1000, type="debit")]
    feasible, entries = simulate(
        initial_balance=0,
        future_ledger=future_ledger,
        cadence_dates=[],
        payment_dates=[],
        payments=[],
        bank_fee_cents=0,
        program_fee_total=0,
        first_payment_date=d,
    )
    assert feasible is False


def test_case1_balance_hits_exactly_zero():
    # The fee-front-loading objective drains the account to exactly $0 while
    # fee collection is still in progress (here: the first two cadence dates).
    client, offer, rules = load_case("cases/case1_feasible_even")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    assert r.schedule[0].balance_cents == 0
    assert all(row.balance_cents >= 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# Horizon limit
# ---------------------------------------------------------------------------

def test_no_schedule_entry_falls_after_horizon():
    client, offer, rules = load_case("cases/case4_tiers")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    assert all(row.date <= client.last_draft_date for row in r.schedule)


def test_first_payment_date_past_horizon_is_infeasible():
    client, offer, rules = load_case("cases/case1_feasible_even")
    offer = Offer(
        creditor=offer.creditor,
        creditor_balance_cents=offer.creditor_balance_cents,
        original_balance_cents=offer.original_balance_cents,
        settlement_pct=offer.settlement_pct,
        first_payment_date=date(client.last_draft_date.year + 1, 1, 1),
    )
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False


# ---------------------------------------------------------------------------
# Fee compliance: no program fee before the first creditor-payment date
# ---------------------------------------------------------------------------

def test_no_fee_collected_before_first_payment_date():
    # first_payment_date is Feb 28 even though Jan 31 is an earlier cadence date
    # with plenty of balance available -- the fee must not be pulled forward.
    jan, feb, mar = date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)
    future_ledger = [
        LedgerEntry(date=jan, amount_cents=5000, type="credit"),
        LedgerEntry(date=feb, amount_cents=5000, type="credit"),
        LedgerEntry(date=mar, amount_cents=5000, type="credit"),
    ]
    feasible, entries = simulate(
        initial_balance=0,
        future_ledger=future_ledger,
        cadence_dates=[jan, feb, mar],
        payment_dates=[feb, mar],
        payments=[1000, 1000],
        bank_fee_cents=0,
        program_fee_total=500,
        first_payment_date=feb,
    )
    assert feasible is True
    # If fee were collected on Jan 31 (before first_payment_date), an entry with
    # fee_collected > 0 would exist on that date -- it must not.
    assert not any(e.date == jan and e.program_fee_cents > 0 for e in entries)


# ---------------------------------------------------------------------------
# Part 2: both minima, including a guardrail-rejection case
# ---------------------------------------------------------------------------

def test_case2_minima_match_expected_values():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    af = r.additional_funds
    assert af.lump_sum.amount_cents == 10000
    assert af.monthly_increment.amount_cents == 2500
    assert af.monthly_increment.num_drafts == 5


def test_severely_underfunded_offer_fails_both_guardrails():
    # A single tiny draft against a large settlement: the extra funding needed
    # dwarfs both guardrail thresholds, so both options must report failure
    # with a non-empty reason.
    client = Client(
        draft_amount_cents=1000,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 1, 1),
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=[LedgerEntry(date=date(2026, 1, 1), amount_cents=1000, type="credit")],
    )
    offer = Offer(
        creditor="BigCo",
        creditor_balance_cents=200000,
        original_balance_cents=200000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 1),
    )
    rules = _rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=100,
        max_token_pays=1,
        min_payment_tiers=[],
        max_segments=1,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    af = r.additional_funds

    assert af.lump_sum.within_guardrail is False
    assert af.lump_sum.reason != ""
    assert af.monthly_increment.within_guardrail is False
    assert af.monthly_increment.reason != ""
