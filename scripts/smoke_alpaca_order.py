import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from alpaca_client import make_clients
from config import load_settings


def compact_order(order) -> dict:
    return {
        "id": str(order.id),
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": str(order.side),
        "qty": str(order.qty),
        "type": str(order.order_type),
        "time_in_force": str(order.time_in_force),
        "status": str(order.status),
        "filled_qty": str(order.filled_qty),
        "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price is not None else None,
        "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
    }


def run(symbol: str, qty: int, cancel_after_submit: bool, wait_seconds: float, force_submit: bool) -> None:
    settings = load_settings()
    if settings.execution_mode != "alpaca_paper" and not force_submit:
        raise SystemExit(
            "Refusing to submit an Alpaca paper order while EXECUTION_MODE is not 'alpaca_paper'. "
            "Set EXECUTION_MODE=alpaca_paper or pass --force-submit."
        )

    clients = make_clients(settings)
    clock = clients.trading.get_clock()
    print(
        json.dumps(
            {
                "paper": settings.alpaca_paper,
                "execution_mode": settings.execution_mode,
                "clock_is_open": clock.is_open,
                "symbol": symbol,
                "qty": qty,
                "cancel_after_submit": cancel_after_submit,
                "force_submit": force_submit,
            },
            indent=2,
            sort_keys=True,
        )
    )

    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id=f"codex-smoke-{symbol.lower()}-{int(time.time())}",
    )
    order = clients.trading.submit_order(order_data=order_request)

    print("Submitted order")
    print(json.dumps(compact_order(order), indent=2, sort_keys=True))

    if wait_seconds > 0:
        time.sleep(wait_seconds)
        order = clients.trading.get_order_by_id(order.id)
        print("Order after wait")
        print(json.dumps(compact_order(order), indent=2, sort_keys=True))

    if cancel_after_submit and str(order.status).lower() not in {"filled", "canceled", "expired"}:
        clients.trading.cancel_order_by_id(order.id)
        order = clients.trading.get_order_by_id(order.id)
        print("Order after cancel")
        print(json.dumps(compact_order(order), indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a tiny Alpaca paper order and optionally cancel it.")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--cancel-after-submit", action="store_true")
    parser.add_argument("--force-submit", action="store_true", help="Override EXECUTION_MODE safety check.")
    parser.add_argument("--wait-seconds", type=float, default=2.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(
        symbol=args.symbol.strip().upper(),
        qty=args.qty,
        cancel_after_submit=args.cancel_after_submit,
        wait_seconds=args.wait_seconds,
        force_submit=args.force_submit,
    )


if __name__ == "__main__":
    main()
