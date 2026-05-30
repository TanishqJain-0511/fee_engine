"""
Payment schedule builders for the three shapes: even, balloon, staircase.

Objective: collect program fee as early as possible → keep creditor payments
as low as the rules allow early on, pushing larger amounts to later dates.
"""
from datetime import date
from typing import List, Optional, Tuple

from .date_utils import round_half_up
from .floors import compute_floors, validate_payments
from .models import CreditorRules


# ---------------------------------------------------------------------------
# Even pays
# ---------------------------------------------------------------------------

def build_even_payments(k: int, offer_total: int, rules: CreditorRules) -> Optional[List[int]]:
    """
    Build k equal (or as-equal-as-possible) payments summing to offer_total.
    Remainder cents go on the LATEST payments (keeps non-decreasing order).
    Returns None if any payment is below its floor.
    """
    base = offer_total // k
    remainder = offer_total % k
    # First (k - remainder) at base, last remainder at base+1
    payments = [base] * (k - remainder) + [base + 1] * remainder
    if not validate_payments(payments, rules, offer_total):
        return None
    return payments


def best_k_even(
    max_installments: int,
    offer_total: int,
    rules: CreditorRules,
) -> List[Tuple[int, List[int]]]:
    """Return all valid (k, payments) pairs for the even shape, k=1..max_installments."""
    candidates = []
    for k in range(1, max_installments + 1):
        p = build_even_payments(k, offer_total, rules)
        if p is not None:
            candidates.append((k, p))
    return candidates


# ---------------------------------------------------------------------------
# Balloon
# ---------------------------------------------------------------------------

def build_balloon_payments(k: int, offer_total: int, rules: CreditorRules) -> Optional[List[int]]:
    """
    Build a balloon schedule: payments 1..k-1 at their floors, payment k absorbs the rest.
    For k=1, the single payment IS the balloon.
    Returns None if infeasible.
    """
    floors = compute_floors(k, rules)

    if k == 1:
        p = offer_total
        if p < floors[0]:
            return None
        return [p]

    early = floors[:-1]
    balloon = offer_total - sum(early)

    # Balloon must be ≥ its own floor and ≥ the last early payment (non-decreasing)
    min_balloon = max(floors[-1], early[-1])
    if balloon < min_balloon:
        return None

    payments = early + [balloon]
    if not validate_payments(payments, rules, offer_total):
        return None
    return payments


def best_k_balloon(
    max_installments: int,
    offer_total: int,
    rules: CreditorRules,
) -> List[Tuple[int, List[int]]]:
    """Return all valid (k, payments) pairs for the balloon shape, k=1..max_installments."""
    candidates = []
    for k in range(1, max_installments + 1):
        p = build_balloon_payments(k, offer_total, rules)
        if p is not None:
            candidates.append((k, p))
    return candidates


# ---------------------------------------------------------------------------
# Staircase
# ---------------------------------------------------------------------------

def _count_distinct(payments: List[int]) -> int:
    return len(set(payments))


def _staircase_candidates_for_k(
    k: int,
    offer_total: int,
    rules: CreditorRules,
) -> List[List[int]]:
    """
    Generate ALL valid staircase payment schedules for exactly k payments.
    We enumerate different 2-level split points and the all-equal case,
    then let the caller (via simulation) pick the most fee-front-loaded one.
    """
    floors = compute_floors(k, rules)
    floor_sum = sum(floors)
    if floor_sum > offer_total:
        return []

    results: List[List[int]] = []
    seen = set()

    def _add(p: List[int]) -> None:
        key = tuple(p)
        if key in seen:
            return
        if _count_distinct(p) > rules.max_segments:
            return
        if validate_payments(p, rules, offer_total):
            seen.add(key)
            results.append(list(p))

    # ── All equal ──────────────────────────────────────────────────────────
    if offer_total % k == 0:
        val = offer_total // k
        if val >= max(floors):
            _add([val] * k)

    # ── Two-level splits (if max_segments >= 2) ────────────────────────────
    # For each split point j (j positions at L1, k-j at L2):
    #   L1 = max(floors[:j])   (lowest possible for first segment)
    #   L2 = remaining / n2    (must be integer and ≥ max(floors[j:], L1))
    if rules.max_segments >= 2:
        for j in range(1, k):
            L1 = max(floors[:j])
            n2 = k - j
            remaining = offer_total - j * L1
            if remaining < 0:
                continue
            min_L2 = max(max(floors[j:]), L1)
            if remaining % n2 == 0:
                L2 = remaining // n2
                if L2 >= min_L2:
                    _add([L1] * j + [L2] * n2)

    # ── Floors + last absorbs excess ───────────────────────────────────────
    # All at their floor except the last position which absorbs all excess.
    # This can produce up to (distinct_floors + 1) levels.
    payments = list(floors)
    payments[-1] += offer_total - floor_sum
    # Must be non-decreasing (last ≥ second-to-last)
    if all(payments[i] >= payments[i - 1] for i in range(1, k)):
        _add(payments)

    return results


def best_k_staircase(
    max_installments: int,
    offer_total: int,
    rules: CreditorRules,
) -> List[Tuple[int, List[int]]]:
    """
    Return all valid (k, payments) candidates for the staircase shape, k=1..max_installments.
    Multiple candidates per k are possible (different split points).
    """
    candidates: List[Tuple[int, List[int]]] = []
    for k in range(1, max_installments + 1):
        for p in _staircase_candidates_for_k(k, offer_total, rules):
            candidates.append((k, p))
    return candidates
