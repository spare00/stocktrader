"""Persist classified signal blocks to trade_journal.jsonl for post-session feedback."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal

from execution import SOURCE_COMMIT

LOG = logging.getLogger(__name__)

BlockCategory = Literal["fishy", "marginal", "data", "risk"]
BlockStage = Literal["strategy_filter", "market_regime", "risk_gate"]

_JOURNAL_COOLDOWN_MS = 60_000
_last_written_ms: dict[tuple[str, str, str, str], int] = {}

# Strategy filter codes that target suspicious / low-quality setups.
_FISHY_FILTER_CODES = frozenset(
    {
        "bp_rejection_wick",
        "stoch_timing",
        "decline_exhaustion",
        "extension",
    }
)

# Quote / feed quality — not a statement about setup merit.
_DATA_FILTER_CODES = frozenset({"spread", "quote"})


def classify_strategy_filter(_strategy: str, code: str, detail: str) -> BlockCategory:
    normalized = code.strip().lower()
    detail_lower = detail.lower()
    if normalized in _DATA_FILTER_CODES or ("spread" in detail_lower and "too wide" in detail_lower):
        return "data"
    if normalized in _FISHY_FILTER_CODES:
        return "fishy"
    if "overbought" in detail_lower and "low-volume" in detail_lower:
        return "fishy"
    if "rejection wick" in detail_lower:
        return "fishy"
    if "needs pullback reclaim" in detail_lower:
        return "fishy"
    if "decline not exhausted" in detail_lower or "decline_exhaustion" in normalized:
        return "fishy"
    return "marginal"


def classify_post_signal(reason: str) -> BlockCategory:
    lower = reason.lower()
    if "market regime panic" in lower:
        return "fishy"
    if "market regime" in lower:
        return "marginal"
    if any(
        token in lower
        for token in (
            "position already open",
            "loss lock",
            "consecutive loss",
            "cooldown",
            "max open positions",
            "daily max loss",
        )
    ):
        return "risk"
    return "marginal"


def write_signal_block(
    *,
    strategy: str,
    symbol: str,
    filter_code: str,
    reason: str,
    stage: BlockStage,
    timestamp_ms: int,
    category: BlockCategory | None = None,
) -> None:
    strategy_name = str(strategy or "").strip()
    symbol_name = str(symbol or "").strip().upper()
    code = str(filter_code or "").strip().lower() or "unknown"
    if not strategy_name or not symbol_name or timestamp_ms <= 0:
        return

    if category is None:
        category = (
            classify_post_signal(reason)
            if stage in {"market_regime", "risk_gate"}
            else classify_strategy_filter(strategy_name, code, reason)
        )

    dedupe_key = (strategy_name, symbol_name, code, stage)
    last_ms = _last_written_ms.get(dedupe_key, -_JOURNAL_COOLDOWN_MS)
    if timestamp_ms - last_ms < _JOURNAL_COOLDOWN_MS:
        return
    _last_written_ms[dedupe_key] = timestamp_ms

    entry = {
        "timestamp": datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(),
        "timestamp_ms": timestamp_ms,
        "event": "signal_blocked",
        "strategy": strategy_name,
        "symbol": symbol_name,
        "block_category": category,
        "block_stage": stage,
        "filter_code": code,
        "reason": reason,
    }
    if SOURCE_COMMIT:
        entry["source_commit"] = SOURCE_COMMIT

    try:
        from execution import TRADE_JOURNAL_FILE

        TRADE_JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TRADE_JOURNAL_FILE.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        LOG.exception("Failed to write signal block for %s %s [%s]", strategy_name, symbol_name, code)


def reset_journal_dedupe_for_tests() -> None:
    _last_written_ms.clear()
