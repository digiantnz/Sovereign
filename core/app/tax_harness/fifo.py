"""Tax Report Harness — FIFO disposal engine (STUB — not yet implemented).

Phase 2 of the Tax Harness produces income.csv and expenses.csv for the accountant.
FIFO disposal calculations (crypto_disposals.csv, gain/loss per disposal) are
planned for a future version.

This module defines the data structures and function signature so callers can
import and call run_fifo() without error. All results will be empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class DisposalResult:
    """Result of a single FIFO-matched disposal. Not yet populated."""
    timestamp:        str
    asset:            str
    amount_disposed:  Decimal
    proceeds_nzd:     Decimal
    cost_basis_nzd:   Decimal
    gain_loss_nzd:    Decimal
    acquisition_date: str
    subtype:          str


@dataclass
class FifoResult:
    """Aggregate result of the FIFO engine. Not yet populated."""
    disposal_results: list[DisposalResult] = field(default_factory=list)
    unresolved:       list = field(default_factory=list)   # disposals with nzd_value None
    data_gaps:        list = field(default_factory=list)   # disposals with insufficient lots


def run_fifo(
    income_events: list,
    disposal_events: list,
) -> FifoResult:
    """FIFO disposal engine — NOT YET IMPLEMENTED.

    When implemented this function will:
      - Process disposal_events in chronological order
      - Consume acquisition lots from income_events oldest-first
      - Use staking reward nzd_value as cost basis
      - Add disposals with nzd_value=None to FifoResult.unresolved
      - Add disposals where lots are exhausted to FifoResult.data_gaps
      - Return a FifoResult with gain/loss per disposal

    Currently returns an empty FifoResult. Crypto disposal calculations
    will be added in Tax Harness Phase 3.
    """
    return FifoResult()
