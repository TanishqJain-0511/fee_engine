# Settlement Feasibility & Fee Engine — Take-home

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests (pass out of the box)
│   └── test_cases.py        # example expectations — make these pass, then add your own
├── run.py                   # python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q
```

Out of the box, `tests/test_smoke.py` passes and `tests/test_cases.py` fails —
the latter is your target. Go beyond those four cases with your own tests.

`tests/test_extra.py` is that "own tests" suite: token-pay and tier floors,
the `max_segments` cap, exact-sum, same-day credit-before-debit ordering, a
balance that hits exactly $0, the horizon limit, no-fee-before-first-payment,
and both Part 2 minima (including a case where both guardrails reject).

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.

---

## Implementation

### New files added

| File | Purpose |
|---|---|
| `feasibility/date_utils.py` | `round_half_up`, `generate_cadence_dates`, `get_first_payment_date`, `advance_month` |
| `feasibility/floors.py` | `compute_floors` (per-position payment minimums), `validate_payments` |
| `feasibility/scheduler.py` | Payment schedule builders: `best_k_even`, `best_k_balloon`, `best_k_staircase` |
| `feasibility/simulator.py` | `simulate()` — walks the ledger date by date and checks feasibility |

`engine.py` was the stub to implement; `models.py` was provided unchanged.

---

### Design decisions

#### Step 1 — Data representation

**Everything is integer cents, never floats.**
All monetary values (balances, payments, fees) are stored and computed as `int` in cents.
The only floats are the percentage fields (`settlement_pct`, `program_fee_pct`) which come from the JSON. They are immediately converted to cents via `round_half_up(pct * balance_cents)` and never used as floats again. This eliminates floating-point drift entirely — two schedules that should produce the same result always will.

**Dates are Python `date` objects, not strings.**
The loaders in `models.py` parse ISO strings into `datetime.date` on load. All internal arithmetic (advance a month, compare to horizon, build cadence sequences) uses `date` objects. `isoformat()` is only called in `Result.to_dict()` when serializing back to JSON.

**`as_of_date` is the strict dividing line.**
Only ledger entries with `entry.date > client.as_of_date` are included in the simulation (`_future_ledger` and `_draft_dates`). Entries on or before `as_of_date` are already settled history — re-applying them would double-count money already in or out of the account.

---

#### Step 2 — Computing targets: offer_total, program_fee, cadence_dates

**offer_total** is what we owe the creditor:
```
offer_total = round_half_up(offer.settlement_pct × offer.creditor_balance_cents)
```
`creditor_balance_cents` is the *creditor* balance (what the client owes the creditor today).
`settlement_pct` is typically < 1 (e.g. 0.50 = settle for 50 cents on the dollar).

**program_fee** is what we (the settlement company) collect:
```
program_fee = round_half_up(rules.program_fee_pct × offer.original_balance_cents)
```
Note it uses `original_balance_cents` — the balance when the client enrolled, *not* the current (reduced) creditor balance. This locks in our fee against the original debt size.

**cadence_dates** are the monthly dates on which payments *can* be made:
```
first_payment_date = offer.first_payment_date  (or EOM of client.first_draft_date if None)
cadence_dates = [first_payment_date, +1 month, +2 months, ..., up to last_draft_date]
```
EOM-snapping rule: if `first_payment_date` is the last day of its month (e.g. Jan 31), every subsequent cadence date also snaps to the last day of its month (Feb 28/29, Mar 31, …). Otherwise the same day-of-month is reused, clamped to the month length (e.g. Jan 31 → Feb 28 is *not* an EOM date, so Mar would be Mar 31 normally but we'd clamp).

`max_installments` — the maximum number of installments we can use — is:
```
max_installments = min(rules.max_payments, rules.max_terms, len(cadence_dates))
```
All three limits must be respected; `len(cadence_dates)` is the hard physical cap (can't schedule more payments than there are cadence dates before the horizon).

---

#### Step 3 — Payment shapes and floor rules

**Shape selection** is driven solely by the creditor flags:
- `even_pays=True` → even shape
- `is_ballooning_allowed=True` (and not even) → balloon shape
- otherwise → staircase shape

**Even**: split offer_total into k equal (or near-equal) installments. Remainder cents go on the *last* (largest) payments to keep the sequence non-decreasing.

**Balloon**: payments 1..k-1 are each at their floor (minimum allowed), payment k absorbs everything left. This front-loads the fee most aggressively — small early payments leave maximum balance for fee collection.

**Staircase**: a constrained shape — payments must be non-decreasing AND have at most `max_segments` distinct values (usually 2). We enumerate valid 2-level splits and pick the one the simulator scores best.

**Floor rules** (applied to every position *i*):
1. **Base min**: `rules.min_payment_cents` — absolute minimum per payment.
2. **Tier step-ups**: `rules.min_payment_tiers` — a list of `(from_payment_number, tier_min)` pairs. For position *i*, the applicable floor is the highest tier whose `from_payment_number ≤ i`.
3. **Token-pay budget**: at most `rules.max_token_pays` payments may sit *exactly* at the base minimum. Once exhausted, the floor steps up to `min_payment_cents + 1`.

The three rules stack: `floor[i] = max(base_min, tier_floor)`, then apply token-pay bump if needed.

---

#### Step 4 — Simulation (the feasibility check)

`simulate()` in `simulator.py` walks every relevant date in sorted order and applies this sequence:

1. **Credits** (drafts + any extra credit) — money arrives first.
2. **Committed debits** (already-scheduled outflows) — balance must stay ≥ 0 after this.
3. **Creditor payment + bank fee** — balance must stay ≥ 0 after this.
4. **Program fee collection** (greedy, only on cadence dates ≥ first_payment_date) — collect as much of the remaining fee as the balance allows, down to zero.

If balance goes negative at any point, or if `fee_remaining > 0` after all dates are processed, the simulation returns `(False, None)`.

**Fee front-loading objective**: `_try_candidates` scores each feasible schedule by `sum(fee_cents[i] * i)` — a weighted sum where later positions carry higher weight. The schedule with the *lowest* score collected the fee earliest and is preferred.

---

#### Step 5 — Minimum extra funding (Part 2)

When the base case is infeasible, we binary-search for the smallest additional input that makes it feasible:

**Lump sum**: a one-time credit `L` placed on the earliest future ledger date (earlier credit is always weakly better — it can fund more cadence dates). Binary search over `[0, offer_total + program_fee + max_installments × bank_fee + 1]`.

**Monthly increment**: a uniform amount `X` added to every future draft. Binary search over the same range. The upper bound intentionally does *not* divide by the number of drafts — some drafts may arrive after the last cadence date and contribute nothing to any obligation, so dividing would underestimate the bound.

**Guardrails**:
- Lump sum: `within_guardrail = L ≤ 0.65 × offer_total`
- Monthly increment: `within_guardrail = X ≤ max(10000, 0.40 × draft_amount_cents)`

---

### Assumptions

- The `as_of_date` field is the strict cutoff: only future ledger entries are simulated.
- `offer.first_payment_date = None` means "use EOM of the client's first draft date."
- When `even_pays` is True, even shape is used regardless of other flags.
- Balloon is tried before staircase when `is_ballooning_allowed=True` and `even_pays=False`.
- The lump sum is placed on the *earliest* future date (conservative/best-case for the client).
- `max_terms` and `max_payments` are both hard caps; the effective limit is their minimum.

### Known edge cases / limitations

- If `first_payment_date > last_draft_date`, the offer is immediately infeasible (no cadence dates fit).
- If there are zero future draft dates, the monthly increment binary search reports `num_drafts=0` and the result is technically the same as a lump sum.
- The staircase enumerator only covers all-equal and 2-level splits. Schedules requiring 3+ levels (if `max_segments ≥ 3`) are not enumerated — a known limitation.
