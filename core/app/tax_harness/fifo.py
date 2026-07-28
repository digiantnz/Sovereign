"""Tax Report Harness — FIFO capital gains engine (Phase 3).

run_fifo() is the single source of truth for:
  - disposal gain/loss per acquisition lot (FifoResult.disposal_results)
  - current holdings cost basis (FifoResult.remaining_lots)

Portfolio costing must derive crypto cost basis from FifoResult.remaining_lots,
not from a separate calculation. See TODO in portfolio_analysis_harness.py.

Disposal events processed: exchange_disposal only.
Known gap: mining_outbound / unknown_outbound could hide unlabelled conversions
(DEX swaps, OTC trades not captured in a Wirex/Swyftx statement CSV). With all
known conversions going through exchange statements this is not a current blind
spot — flag immediately if any off-statement conversion ever occurs.

Acquisition lots: mining_income, staking_reward, exchange_acquisition.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


_ACQUISITION_SUBTYPES = frozenset({"mining_income", "staking_reward", "exchange_acquisition"})
_DISPOSAL_SUBTYPES    = frozenset({"exchange_disposal"})


@dataclass
class AcquisitionLot:
    """One acquired block of an asset, tracked through FIFO matching.

    cost_basis_nzd is None when the lot was unpriced after on-the-fly enrichment.
    Portfolio consumers must treat None as an unknown cost basis, not zero.
    The fraction still held is remaining / amount.
    """
    timestamp:      str             # ISO8601 UTC acquisition date
    asset:          str             # e.g. "ETH"
    amount:         Decimal         # original quantity acquired
    remaining:      Decimal         # quantity not yet matched against a disposal
    cost_basis_nzd: Decimal | None  # NZD value for the full original amount; None if unpriced
    subtype:        str             # mining_income | staking_reward | exchange_acquisition
    reference:      str             # tx hash / CSV row ref — for audit


@dataclass
class DisposalResult:
    """One lot-level FIFO match within a disposal event.

    A single disposal spanning N lots produces N DisposalResult rows,
    all sharing the same timestamp and reference.
    """
    timestamp:           str      # disposal date ISO8601
    asset:               str
    amount_disposed:     Decimal  # portion of the disposal matched to this lot
    proceeds_nzd:        Decimal  # proportional proceeds for this portion
    cost_basis_nzd:      Decimal  # FIFO cost basis for this portion
    gain_loss_nzd:       Decimal  # proceeds_nzd - cost_basis_nzd
    acquisition_date:    str      # YYYY-MM-DD of lot acquisition
    acquisition_subtype: str      # mining_income | staking_reward | exchange_acquisition
    subtype:             str      # disposal subtype (exchange_disposal)
    reference:           str      # disposal reference for audit trail


@dataclass
class FifoResult:
    """Complete FIFO engine output."""
    # matched disposal rows — one entry per lot consumed per disposal
    disposal_results: list[DisposalResult] = field(default_factory=list)
    # disposals where proceeds NZD is missing — gain/loss cannot be computed
    unresolved:       list[dict]           = field(default_factory=list)
    # disposals (or portions) with no matching lot, or lots with unknown cost basis
    data_gaps:        list[dict]           = field(default_factory=list)
    # unconsumed lot balances after all disposals — portfolio cost basis source of truth
    remaining_lots:   list[AcquisitionLot] = field(default_factory=list)


def _parse_amount(amount_str: str | None) -> Decimal | None:
    """Parse "0.123456 ETH" → Decimal, or None on failure."""
    if not amount_str:
        return None
    try:
        return Decimal(amount_str.split()[0].replace(",", ""))
    except (InvalidOperation, IndexError):
        return None


def _parse_nzd(nzd_str: str | None) -> Decimal | None:
    """Parse "$X.XX NZD" → Decimal, or None on failure."""
    if not nzd_str:
        return None
    try:
        return Decimal(nzd_str.replace("$", "").replace("NZD", "").replace(",", "").strip())
    except InvalidOperation:
        return None


def run_fifo(income_events: list, disposal_events: list) -> FifoResult:
    """FIFO capital gains engine.

    Parameters
    ----------
    income_events   : classified TaxEvent objects (any subtype); only
                      ACQUISITION_SUBTYPES are consumed as lots.
    disposal_events : classified TaxEvent objects with subtype in
                      DISPOSAL_SUBTYPES.

    Returns
    -------
    FifoResult.  One DisposalResult row per lot consumed per disposal.
    Unpriced lots (cost_basis_nzd=None) land in data_gaps and are excluded
    from disposal_results — the accountant must supply the cost basis manually.
    """
    # ── Build lot pool ────────────────────────────────────────────────────────
    lots_by_asset: dict[str, list[AcquisitionLot]] = defaultdict(list)

    for ev in income_events:
        if (ev.subtype or "") not in _ACQUISITION_SUBTYPES:
            continue
        amount = _parse_amount(ev.amount)
        if not amount or amount <= 0:
            continue
        asset = (ev.asset or "ETH").upper()
        lots_by_asset[asset].append(AcquisitionLot(
            timestamp=ev.timestamp or "",
            asset=asset,
            amount=amount,
            remaining=amount,
            cost_basis_nzd=_parse_nzd(ev.nzd_value),  # None if unpriced
            subtype=ev.subtype or "",
            reference=ev.reference or "",
        ))

    # Sort each asset's lots oldest-first (FIFO)
    for asset_lots in lots_by_asset.values():
        asset_lots.sort(key=lambda l: l.timestamp)

    # ── Match disposals ───────────────────────────────────────────────────────
    result = FifoResult()

    filtered_disposals = sorted(
        (ev for ev in disposal_events if (ev.subtype or "") in _DISPOSAL_SUBTYPES),
        key=lambda e: e.timestamp or "",
    )

    for disposal in filtered_disposals:
        asset          = (disposal.asset or "ETH").upper()
        proceeds_total = _parse_nzd(disposal.nzd_value)
        amount_total   = _parse_amount(disposal.amount)

        if proceeds_total is None or amount_total is None or amount_total <= 0:
            result.unresolved.append({
                "reference": disposal.reference or "",
                "timestamp": disposal.timestamp or "",
                "asset":     asset,
                "amount":    disposal.amount or "",
                "reason":    "missing_proceeds_nzd",
            })
            continue

        lots        = lots_by_asset.get(asset, [])
        amount_left = amount_total

        for lot in lots:
            if lot.remaining <= 0 or amount_left <= 0:
                continue

            take = min(lot.remaining, amount_left)

            if lot.cost_basis_nzd is None:
                # Unpriced lot — gap recorded, lot still consumed, no DisposalResult row
                result.data_gaps.append({
                    "reference":        disposal.reference or "",
                    "timestamp":        disposal.timestamp or "",
                    "asset":            asset,
                    "amount":           str(take),
                    "acquisition_date": lot.timestamp[:10] if lot.timestamp else "",
                    "acquisition_ref":  lot.reference,
                    "reason":           "unpriced_acquisition_lot",
                })
            else:
                per_unit_cost = lot.cost_basis_nzd / lot.amount
                proceeds_prop = (take / amount_total) * proceeds_total
                portion_cost  = per_unit_cost * take

                result.disposal_results.append(DisposalResult(
                    timestamp=disposal.timestamp or "",
                    asset=asset,
                    amount_disposed=take,
                    proceeds_nzd=proceeds_prop,
                    cost_basis_nzd=portion_cost,
                    gain_loss_nzd=proceeds_prop - portion_cost,
                    acquisition_date=lot.timestamp[:10] if lot.timestamp else "",
                    acquisition_subtype=lot.subtype,
                    subtype=disposal.subtype or "",
                    reference=disposal.reference or "",
                ))

            lot.remaining -= take
            amount_left   -= take

        if amount_left > Decimal("0.000001"):
            result.data_gaps.append({
                "reference": disposal.reference or "",
                "timestamp": disposal.timestamp or "",
                "asset":     asset,
                "amount":    str(amount_left),
                "reason":    "insufficient_lots",
            })

    # ── Remaining lots — portfolio cost basis source of truth ─────────────────
    result.remaining_lots = sorted(
        (lot for asset_lots in lots_by_asset.values() for lot in asset_lots if lot.remaining > 0),
        key=lambda l: l.timestamp,
    )

    return result
