import json
import logging

from openai import OpenAI

from config import Settings
from models import Signal


LOG = logging.getLogger(__name__)


class SignalReviewer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def review(self, signal: Signal) -> str | None:
        if not self.settings.ai_review or not self.client:
            return None

        payload = {
            "symbol": signal.symbol,
            "side": signal.side,
            "price": signal.price,
            "change_pct": signal.change_pct,
            "volume_ratio": signal.volume_ratio,
            "spread_bps": signal.spread_bps,
            "reason": signal.reason,
        }

        try:
            response = self.client.responses.create(
                model=self.settings.openai_model,
                instructions=(
                    "You are reviewing a seconds-level paper-trading signal. "
                    "Return one concise sentence explaining the strongest risk or confirmation. "
                    "Do not give financial advice and do not tell the system to trade."
                ),
                input=json.dumps(payload, sort_keys=True),
            )
            return response.output_text.strip()
        except Exception:
            LOG.exception("OpenAI signal review failed")
            return None
