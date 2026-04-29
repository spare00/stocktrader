import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_agent import SignalReviewer
from config import load_settings
from models import Signal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test the OpenAI signal-review path.")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--strategy", default="opening_impulse")
    parser.add_argument("--price", type=float, default=187.25)
    parser.add_argument("--change-pct", type=float, default=0.0125)
    parser.add_argument("--volume-ratio", type=float, default=2.4)
    parser.add_argument("--spread-bps", type=float, default=6.5)
    parser.add_argument("--reason", default="opening impulse 1.25% over 40s, volume 2.4x baseline")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings(strategy_names=[])
    reviewer = SignalReviewer(settings)

    signal = Signal(
        strategy=args.strategy,
        symbol=args.symbol.strip().upper(),
        side="BUY",
        price=args.price,
        timestamp_ms=int(time.time() * 1000),
        change_pct=args.change_pct,
        volume_ratio=args.volume_ratio,
        spread_bps=args.spread_bps,
        reason=args.reason,
    )

    result = reviewer.review(signal)
    print(
        json.dumps(
            {
                "ai_review_enabled": settings.ai_review,
                "model": settings.openai_model,
                "signal": signal.__dict__,
                "review": result,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
