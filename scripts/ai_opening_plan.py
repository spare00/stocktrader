import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_SCREEN_FILE = Path("data/opening_screen.json")
DEFAULT_OUTPUT_FILE = Path("data/opening_plan.json")
MIN_CLOSE_CAPTURE_RATIO = 0.1
MIN_POSITIVE_CLOSE_DAY_RATIO = 0.5
MIN_MEDIAN_OPENING_CLOSE_BPS = 0.0


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    result, _ = decoder.raw_decode(stripped)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object.")
    return result


def load_screen(path: Path) -> dict[str, Any]:
    return extract_json_object(path.read_text())


def load_universe(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [part.strip().upper() for part in path.read_text().replace("\n", ",").split(",") if part.strip()]


def candidate_rejection_reason(candidate: dict[str, Any]) -> str | None:
    close_capture = float(candidate.get("close_capture_ratio", 0.0) or 0.0)
    positive_ratio = float(candidate.get("positive_close_day_ratio", 0.0) or 0.0)
    median_close_bps = float(candidate.get("median_opening_close_move_bps", 0.0) or 0.0)
    fade_bps = float(candidate.get("fade_bps", 0.0) or 0.0)
    reasons = []
    if median_close_bps < MIN_MEDIAN_OPENING_CLOSE_BPS:
        reasons.append(f"median_opening_close_move_bps={median_close_bps:.1f}")
    if close_capture < MIN_CLOSE_CAPTURE_RATIO:
        reasons.append(f"close_capture={close_capture:.3f}")
    if positive_ratio < MIN_POSITIVE_CLOSE_DAY_RATIO:
        reasons.append(f"positive_close_day_ratio={positive_ratio:.3f}")
    if not reasons:
        return None
    reasons.append(f"fade_bps={fade_bps:.1f}")
    return "follow-through weak: " + ", ".join(reasons)


def plan_from_screen(screen: dict[str, Any], limit: int) -> dict[str, Any]:
    candidates = list(screen.get("candidates") or [])
    keep = []
    rejected = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol", "")).upper()
        reason = candidate_rejection_reason(candidate)
        if reason is None:
            keep.append(symbol)
        else:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": reason,
                }
            )

    selected = keep[:limit]
    selected = [symbol for symbol in selected if symbol]
    settings = {
        "MAX_OPEN_POSITIONS": 1 if len(selected) <= 2 else 2,
    }
    return {
        "date": str(screen.get("as_of", date.today().isoformat()))[:10],
        "strategy": "opening_impulse",
        "symbols": selected,
        "rejected": rejected,
        "settings": settings,
        "risk_note": "Generated from deterministic screen fallback; no-trade is acceptable when screened candidates lack opening follow-through.",
    }


def ai_plan(settings: Settings, screen: dict[str, Any], universe_symbols: list[str], limit: int) -> dict[str, Any] | None:
    if not settings.openai_api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    payload = {
        "screen": screen,
        "universe_symbols": universe_symbols,
        "limit": limit,
        "allowed_settings": {
            "OPENING_IMPULSE_CHANGE_PCT": [0.003, 0.02],
            "OPENING_IMPULSE_VOLUME_RATIO": [1.5, 5.0],
            "MAX_OPEN_POSITIONS": [0, settings.max_open_positions],
            "MAX_POSITION_VALUE": [0, settings.max_position_value],
            "TARGET_PROFIT_PCT": [0.003, 0.02],
            "STOP_LOSS_PCT": [0.002, settings.stop_loss_pct],
        },
    }
    response = client.responses.create(
        model=settings.openai_model,
        instructions=(
            "Create a conservative pre-market plan for a seconds-level paper-trading opening_impulse strategy. "
            "Return only JSON. Include keys: date, strategy, symbols, rejected, settings, risk_note. "
            "The symbols value must be an array of ticker strings, not objects. "
            "Put symbol explanations in rejected or risk_note, not in symbols. "
            "Symbols must come from screen.candidates. Use settings only from allowed_settings. "
            "Prefer rejecting spike-and-fade candidates with weak close_capture_ratio or negative close movement. "
            "Never increase MAX_OPEN_POSITIONS or MAX_POSITION_VALUE beyond allowed ranges."
        ),
        input=json.dumps(payload, sort_keys=True),
    )
    return extract_json_object(response.output_text)


def validated_plan(plan: dict[str, Any], screen: dict[str, Any], limit: int) -> dict[str, Any]:
    candidates = {str(candidate.get("symbol", "")).upper(): candidate for candidate in screen.get("candidates") or []}
    selected = []
    rejected = list(plan.get("rejected") or [])
    rejected_symbols = {str(item.get("symbol", "")).upper() for item in rejected if isinstance(item, dict)}

    for raw_symbol in plan.get("symbols") or []:
        symbol = str(raw_symbol).upper()
        candidate = candidates.get(symbol)
        if not symbol:
            continue
        if candidate is None:
            if symbol not in rejected_symbols:
                rejected.append({"symbol": symbol, "reason": "not present in latest screen.candidates"})
            continue
        reason = candidate_rejection_reason(candidate)
        if reason is not None:
            if symbol not in rejected_symbols:
                rejected.append({"symbol": symbol, "reason": reason})
            continue
        if symbol not in selected:
            selected.append(symbol)
        if len(selected) >= limit:
            break

    plan["symbols"] = selected
    plan["rejected"] = rejected
    return plan


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    settings_kwargs = {}
    if args.alpaca_api_key:
        settings_kwargs["alpaca_api_key"] = args.alpaca_api_key
    if args.alpaca_secret_key:
        settings_kwargs["alpaca_secret_key"] = args.alpaca_secret_key
    if args.openai_api_key is not None:
        settings_kwargs["openai_api_key"] = args.openai_api_key
    settings = Settings(**settings_kwargs)

    screen = load_screen(args.screen_file)
    universe_symbols = load_universe(args.universe_file)
    plan = ai_plan(settings, screen, universe_symbols, args.limit)
    if plan is None:
        plan = plan_from_screen(screen, args.limit)
    else:
        plan = validated_plan(plan, screen, args.limit)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a bounded AI pre-market opening plan.")
    parser.add_argument("--universe-file", type=Path, default=DEFAULT_UNIVERSE_FILE)
    parser.add_argument("--screen-file", type=Path, default=DEFAULT_SCREEN_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--openai-api-key", default=None)
    parser.add_argument("--alpaca-api-key", default=None)
    parser.add_argument("--alpaca-secret-key", default=None)
    return parser.parse_args()


def main() -> None:
    plan = build_plan(parse_args())
    print(json.dumps(plan, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
