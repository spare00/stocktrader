import json
import logging

from config import Settings


LOG = logging.getLogger(__name__)


def build_openai_client(settings: Settings):
    if not settings.openai_api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        LOG.warning("OpenAI package is not installed; AI features disabled")
        return None

    return OpenAI(api_key=settings.openai_api_key)


def request_json_response(settings: Settings, instructions: str, payload: dict):
    client = build_openai_client(settings)
    if client is None:
        return None

    try:
        response = client.responses.create(
            model=settings.openai_model,
            instructions=instructions,
            input=json.dumps(payload, sort_keys=True),
        )
    except Exception:
        LOG.exception("OpenAI request failed")
        return None
    return response.output_text
