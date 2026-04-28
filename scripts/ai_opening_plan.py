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


def candidate_penalty(candidate: dict[str, Any]) -> tuple[float, list[str]]:
    close_capture = float(candidate.get("close_capture_ratio", 0.0) or 0.0)
    positive_ratio = float(candidate.get("positive_close_day_ratio", 0.0) or 0.0)
    median_close_bps = float(candidate.get("median_opening_close_move_bps", 0.0) or 0.0)
    penalty = 0.0
    notes = []
    if median_close_bps < MIN_MEDIAN_OPENING_CLOSE_BPS:
        penalty += 1.0
        notes.append("negative follow-through")
    if close_capture < MIN_CLOSE_CAPTURE_RATIO:
        penalty += 1.0
        notes.append("weak close capture")
    if positive_ratio < MIN_POSITIVE_CLOSE_DAY_RATIO:
        penalty += 1.0
        notes.append("low positive close ratio")
    return penalty, notes


def ranked_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol", "")).upper()
        if not symbol:
            continue
        base_score = float(candidate.get("score", 0.0) or 0.0)
        penalty, notes = candidate_penalty(candidate)
        ranked.append(
            {
                "symbol": symbol,
                "score": round(base_score - penalty, 3),
                "base_score": round(base_score, 3),
                "penalty": round(penalty, 3),
                "notes": notes,
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def plan_from_screen(screen: dict[str, Any], limit: int) -> dict[str, Any]:
    candidates = list(screen.get("candidates") or [])
    ranked = ranked_candidates(candidates)
    selected = [item["symbol"] for item in ranked[:limit]]
    settings = {
        "MAX_OPEN_POSITIONS": 1 if len(selected) <= 2 else 2,
    }
    return {
        "date": str(screen.get("as_of", date.today().isoformat()))[:10],
        "strategy": "opening_impulse",
        "symbols": selected,
        "ranked": ranked[:limit],
        "rejected": [],
        "settings": settings,
        "risk_note": "Ranking-based selection; no strict filtering applied.",
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
            "Rank candidates instead of rejecting them. Return only JSON. "
            "Include keys: date, strategy, symbols, ranked, rejected, settings, risk_note. "
            "The symbols value must be an array of ticker strings, not objects. "
            "Put symbol ranking details in ranked, not in symbols. "
            "Symbols must come from screen.candidates. Use settings only from allowed_settings. "
            "Do not eliminate symbols based on historical metrics. "
            "Focus on current momentum, opening structure, and intraday opportunity. "
            "Return top symbols ranked by expected opportunity, up to the requested limit when candidates are available. "
            "Never increase MAX_OPEN_POSITIONS or MAX_POSITION_VALUE beyond allowed ranges."
        ),
        input=json.dumps(payload, sort_keys=True),
    )
    return extract_json_object(response.output_text)


def validated_plan(plan: dict[str, Any], screen: dict[str, Any], limit: int) -> dict[str, Any]:
    candidates = {str(candidate.get("symbol", "")).upper(): candidate for candidate in screen.get("candidates") or []}
    fallback_ranked = ranked_candidates(list(screen.get("candidates") or []))
    selected = []
    rejected = []

    for raw_symbol in plan.get("symbols") or []:
        symbol = str(raw_symbol).upper()
        candidate = candidates.get(symbol)
        if not symbol:
            continue
        if candidate is None:
            continue
        if symbol not in selected:
            selected.append(symbol)
        if len(selected) >= limit:
            break

    for item in fallback_ranked:
        if len(selected) >= limit:
            break
        if item["symbol"] not in selected:
            selected.append(item["symbol"])

    ranked_by_symbol = {item["symbol"]: item for item in fallback_ranked}
    ranked = [ranked_by_symbol[symbol] for symbol in selected if symbol in ranked_by_symbol]
    plan["symbols"] = selected
    plan["ranked"] = ranked
    plan["rejected"] = rejected
    plan["risk_note"] = "Ranking-based selection; no strict filtering applied."
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
