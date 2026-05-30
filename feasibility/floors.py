"""
Floor computation and payment validation for creditor payment schedules.

The floor for each payment position is determined by three stacked rules
(highest wins):
  1. Base minimum: min_payment_cents
  2. Token-pay budget: at most max_token_pays payments may sit exactly at the
     base minimum; once exhausted the floor steps up to min_payment_cents + 1
  3. Tier step-ups: min_payment_tiers raises the floor from a given payment
     number onward
"""
from typing import List

from .models import CreditorRules


def compute_floors(k: int, rules: CreditorRules) -> List[int]:
    """
    Return the minimum payment floor for each of the k positions (1-indexed internally,
    but returned as a 0-indexed list).

    The floor for position i is:
      max(min_payment_cents, applicable tier min)
    with the additional token-pay constraint: if a position's floor equals
    min_payment_cents AND we've already used all max_token_pays token pays,
    the floor is bumped to min_payment_cents + 1.

    We assume each position is assigned exactly its floor when counting token pays,
    which is the conservative choice for front-loading (lowest possible early payments).
    """
    floors: List[int] = []
    token_pays_used = 0

    for i in range(1, k + 1):
        # Tier floor: highest tier whose from_payment_number <= i
        tier_floor = rules.min_payment_cents
        for from_num, tier_min in rules.min_payment_tiers:
            if i >= from_num:
                tier_floor = max(tier_floor, tier_min)

        if tier_floor > rules.min_payment_cents:
            # Tier forces strictly above base min → not a token pay
            floor = tier_floor
        elif token_pays_used >= rules.max_token_pays:
            # Token pays exhausted → must exceed base min
            floor = rules.min_payment_cents + 1
        else:
            # At base min → token pay consumed
            floor = rules.min_payment_cents
            token_pays_used += 1

        floors.append(floor)

    return floors


def validate_payments(payments: List[int], rules: CreditorRules, offer_total: int) -> bool:
    """
    Check all hard constraints except balance feasibility:
      - k within [1, min(max_payments, max_terms)]
      - exact sum
      - non-decreasing
      - floors (base min, token-pay, tiers)
    """
    k = len(payments)
    if k < 1 or k > min(rules.max_payments, rules.max_terms):
        return False
    if sum(payments) != offer_total:
        return False
    token_pays = 0
    for i, p in enumerate(payments, 1):
        if i > 1 and p < payments[i - 2]:
            return False  # not non-decreasing
        tier_floor = rules.min_payment_cents
        for from_num, tier_min in rules.min_payment_tiers:
            if i >= from_num:
                tier_floor = max(tier_floor, tier_min)
        if p < tier_floor:
            return False
        if p == rules.min_payment_cents:
            if token_pays >= rules.max_token_pays:
                return False
            token_pays += 1
    return True
