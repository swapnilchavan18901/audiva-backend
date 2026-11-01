"""Per-request LLM benchmark logging. Costs are estimates in INR."""

import json
import logging
import time
from contextvars import ContextVar
from uuid import uuid4

from app.constants import constants

logger = logging.getLogger(__name__)


def configure_logging():
    """Make usage records visible with the default Uvicorn logging setup."""
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.propagate = False


class Benchmark:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.session_id = str(uuid4())
        self.turn = 0  # Greeting or background work before a user turn.
        self._turn_counter = 0
        self.context_turn = ContextVar(
            f"benchmark_turn_{self.session_id}", default=None
        )

    def next_turn(self):
        self._turn_counter += 1
        self.turn = self._turn_counter
        return self.turn

    def begin(self, stage, model, operation=None):
        turn = self.context_turn.get()
        return {
            "request_id": str(uuid4()),
            "turn": self.turn if turn is None else turn,
            "stage": stage,
            "model": model,
            "operation": operation or stage,
            "_started": time.monotonic(),
        }

    def first_chunk(self, request):
        request.setdefault("_first_chunk", time.monotonic())

    def first_token(self, request):
        request.setdefault("_first_token", time.monotonic())

    def response_finished(self, request):
        request["_finished"] = time.monotonic()

    def finish(
        self,
        request,
        *,
        usage=None,
        status="completed",
    ):
        prompt = usage.get("prompt_tokens") if usage else None
        completion = usage.get("completion_tokens") if usage else None
        cached = (
            ((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            if usage
            else 0
        )
        cost = None
        input_cost = cached_cost = output_cost = None
        if prompt is not None and completion is not None:
            cached = min(cached, prompt)
            input_cost = (
                (prompt - cached) * constants.SARVAM_LLM_INPUT_PER_MILLION / 1_000_000
            )
            cached_cost = cached * constants.SARVAM_LLM_CACHED_PER_MILLION / 1_000_000
            output_cost = (
                completion * constants.SARVAM_LLM_OUTPUT_PER_MILLION / 1_000_000
            )
            cost = input_cost + cached_cost + output_cost
        finished = request.get("_finished", time.monotonic())

        def elapsed(timestamp):
            return (
                round((timestamp - request["_started"]) * 1000, 2)
                if timestamp is not None
                else None
            )

        record = {
            **{key: value for key, value in request.items() if not key.startswith("_")},
            "duration_ms": elapsed(finished),
            "time_to_first_chunk_ms": elapsed(request.get("_first_chunk")),
            "time_to_first_token_ms": elapsed(request.get("_first_token")),
            "status": status,
            "input_tokens": prompt,
            "cached_tokens": cached if prompt is not None else None,
            "output_tokens": completion,
            "total_tokens": usage.get("total_tokens") if usage else None,
            "reasoning_tokens": (
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                if usage
                else None
            ),
            "provider_usage": usage,
            "usage_source": "sarvam_response" if usage else "not_reported",
            "cost_source": "published_sarvam_rates_estimate",
            "estimated_input_cost_inr": round(input_cost, 8)
            if input_cost is not None
            else None,
            "estimated_cached_cost_inr": round(cached_cost, 8)
            if cached_cost is not None
            else None,
            "estimated_output_cost_inr": round(output_cost, 8)
            if output_cost is not None
            else None,
            "estimated_cost_inr": round(cost, 8) if cost is not None else None,
        }
        # No prompts, transcripts, phone numbers or credentials are logged.
        logger.info(
            "ai_benchmark %s",
            json.dumps(
                {
                    "session_id": self.session_id,
                    "pipeline": self.pipeline,
                    **record,
                }
            ),
        )
        return record
