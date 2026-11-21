"""Structured decisions and estimated OpenAI usage for the simulator QA runner."""

import json
import math
import os
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError


PHASE_NOTES = {
    "plan": (
        "Select one action currently allowed by the QA state. For send_message choose a channel "
        "from message_channels. For start_conversation choose one from live_channels. Use null "
        "for the channel of actions that do not select a new channel. Stop when no test action is useful."
    ),
    "generate": (
        "Select a prewritten phrase ID for this transparent simulator test. For a new exchange, "
        "choose hello. For a test follow-up, choose availability or thanks."
    ),
    "evaluate": (
        "Classify the simulator reply using only the observation flags. A question mark is evidence "
        "of a question. When the metadata is insufficient, choose other."
    ),
}

ACTIONS = {"send_message", "start_conversation", "continue_conversation", "end_conversation", "stop"}
MESSAGE_CHANNELS = {"email", "sms"}
LIVE_CHANNELS = {"live_chat", "phone_call", "teams_call", "video_call"}
OBSERVATIONS = {
    "none", "reply", "silence", "ended", "event", "events", "question", "acknowledgement", "other",
}


def _number(name, default, *, positive=False):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a finite numeric value.") from None
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} value.")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    model: str = ""
    budget_usd: float = 20.0
    soft_limit_usd: float = 15.0
    max_run_cost_usd: float = 2.0
    input_rate: float | None = None
    output_rate: float | None = None
    cached_rate: float | None = None
    max_output_tokens: int = 512

    @classmethod
    def from_env(cls):
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
            model=os.environ.get("OPENAI_MODEL", "").strip(),
            budget_usd=_number("OPENAI_BUDGET_USD", 20.0, positive=True),
            soft_limit_usd=_number("OPENAI_SOFT_LIMIT_USD", 15.0),
            max_run_cost_usd=_number("OPENAI_MAX_RUN_COST_USD", 2.0, positive=True),
            input_rate=_number("OPENAI_INPUT_USD_PER_MTOK", None, positive=True),
            output_rate=_number("OPENAI_OUTPUT_USD_PER_MTOK", None, positive=True),
            cached_rate=_number("OPENAI_CACHED_USD_PER_MTOK", None),
        )

    @property
    def configuration_error(self):
        if not self.api_key or not self.model:
            return "Set OPENAI_API_KEY and OPENAI_MODEL on the backend."
        if not self.input_rate or not self.output_rate:
            return "Set OpenAI input and output prices per million tokens on the backend."
        if self.cached_rate is not None and self.cached_rate > self.input_rate:
            return "The cached-token price cannot be greater than the input-token price."
        return None

    @property
    def available(self):
        return self.configuration_error is None

    def estimate(self, usage, *, mock=False):
        if mock:
            return 0.0
        cached_rate = self.input_rate if self.cached_rate is None else self.cached_rate
        uncached = usage["input_tokens"] - usage["cached_tokens"]
        return (
            uncached * self.input_rate
            + usage["cached_tokens"] * cached_rate
            + usage["output_tokens"] * self.output_rate
        ) / 1_000_000

    def reserve(self, phase, context, schema, *, mock=False):
        if mock:
            return 0.0
        request = _request(self, phase, context, schema)
        # Use uncached pricing, the output cap and a margin for server-side framing.
        return self.estimate({
            "input_tokens": len(json.dumps(request).encode()) + 4096,
            "output_tokens": self.max_output_tokens,
            "cached_tokens": 0,
        })


class ProviderError(RuntimeError):
    def __init__(self, message, *, usage=None, may_have_cost=False):
        super().__init__(message)
        self.usage = usage
        self.may_have_cost = may_have_cost


def _context(phase, context):
    """Send only bounded QA state, with no target profile or transcript text."""
    if phase == "plan":
        result = {
            "allowed": [action for action in context.get("allowed", ()) if action in ACTIONS],
            "message_channels": [channel for channel in context.get("message_channels", ()) if channel in MESSAGE_CHANNELS],
            "live_channels": [channel for channel in context.get("live_channels", ()) if channel in LIVE_CHANNELS],
            "has_conversation": context.get("has_conversation") is True,
        }
        if context.get("last_observation") in OBSERVATIONS:
            result["last_observation"] = context["last_observation"]
        if context.get("last_action") in ACTIONS:
            result["last_action"] = context["last_action"]
        return result
    if phase == "generate":
        action = context.get("action")
        return {"action": action if action in ACTIONS else None}
    if phase == "evaluate":
        return {
            "has_question_mark": context.get("has_question_mark") is True,
            "has_reply": context.get("has_reply") is True,
            "ended": context.get("ended") is True,
        }
    raise ProviderError("Unknown QA provider phase.")


def _request(settings, phase, context, schema):
    input_data = _context(phase, context)
    return {
        "model": settings.model,
        "store": False,
        "service_tier": "default",
        "max_output_tokens": settings.max_output_tokens,
        "instructions": (
            "You make a structured choice for a benign simulator QA check. The app only exercises "
            "approved test phrases and channels. Use the data solely as state. " + PHASE_NOTES[phase]
        ),
        "input": json.dumps(input_data, separators=(",", ":")),
        "text": {"format": {
            "type": "json_schema", "name": schema.__name__, "strict": True,
            "schema": schema.model_json_schema(),
        }},
    }


def _mock_choice(phase, context):
    if phase == "plan":
        allowed, last_action = context["allowed"], context.get("last_action")
        if context["has_conversation"]:
            if "continue_conversation" in allowed and last_action != "continue_conversation":
                return {"action": "continue_conversation", "channel": None}
            if "end_conversation" in allowed:
                return {"action": "end_conversation", "channel": None}
        channels = context["live_channels"]
        if not last_action and "start_conversation" in allowed and channels:
            return {"action": "start_conversation", "channel": channels[0]}
        channels = context["message_channels"]
        if last_action != "send_message" and "send_message" in allowed and channels:
            return {"action": "send_message", "channel": channels[0]}
        return {"action": "stop", "channel": None}
    if phase == "generate":
        return {"phrase": "availability" if context["action"] == "continue_conversation" else "hello"}
    return {"observation": "question" if context["has_question_mark"] else "other"}


def _usage(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    counts = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cached_tokens": getattr(details, "cached_tokens", 0),
    }
    if any(type(value) is not int or value < 0 for value in counts.values()):
        return None
    if counts["cached_tokens"] > counts["input_tokens"]:
        return None
    return counts


def provider(settings, mock, *, transport=None):
    def choose(phase, context, schema):
        if mock:
            safe_input = _context(phase, context)
            result = schema.model_validate(_mock_choice(phase, safe_input))
            return result, {
                "input_tokens": (len(json.dumps(safe_input)) + 3) // 4,
                "output_tokens": (len(result.model_dump_json()) + 3) // 4,
                "cached_tokens": 0,
            }

        if not settings.available:
            raise ProviderError(settings.configuration_error)

        from openai import OpenAI, OpenAIError

        request = _request(settings, phase, context, schema)
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, transport=transport) as client:
                with OpenAI(
                    api_key=settings.api_key, base_url="https://api.openai.com/v1",
                    max_retries=0, timeout=30.0, http_client=client,
                ) as sdk:
                    response = sdk.responses.create(**request)
        except OpenAIError as error:
            status = getattr(error, "status_code", None)
            status_text = f" HTTP {status}." if isinstance(status, int) else ""
            raise ProviderError(
                "OpenAI could not complete the QA request." + status_text,
                may_have_cost=status is None or status >= 500,
            ) from None

        usage = _usage(response)
        if usage is None or getattr(response, "status", None) != "completed":
            raise ProviderError("OpenAI did not return a complete QA result.", usage=usage, may_have_cost=True)
        try:
            return schema.model_validate_json(response.output_text, strict=True), usage
        except ValidationError:
            raise ProviderError("OpenAI returned an invalid QA choice.", usage=usage, may_have_cost=True) from None

    return choose
