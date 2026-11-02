"""Twilio Voice REST adapter. Never retry a mutating request automatically."""

import asyncio
import time
from urllib.parse import quote, urlsplit

import httpx

from app.clients.twilio_logging import logger, safe_detail


def call_status(value):
    """Normalize Twilio's single-l canceled spelling for persisted statuses."""
    return {
        "canceled": "cancelled",
        "initiated": "queued",
        "answered": "in-progress",
    }.get(value, value)


def callback_status(payload):
    return call_status(payload.get("CallStatus"))


def rejection_message(exc):
    """Translate the restricted Voice trial rejection without exposing raw payloads."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
            message = str(payload.get("message", "")).lower()
        except (ValueError, AttributeError):
            message = ""
        if "trial" in message and "limited parameter access" in message:
            return (
                "Twilio trial restrictions block this AI phone integration. "
                "Media Streams requires an upgraded account. Use browser voice to test."
            )
    return "Twilio rejected the call request"


class TwilioClient:
    def __init__(self, settings):
        self.settings = settings
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self.http = httpx.AsyncClient(
            base_url=f"https://api.twilio.com/2010-04-01/Accounts/{quote(settings.twilio_account_sid, safe='')}/",
            auth=(
                settings.twilio_account_sid,
                settings.twilio_auth_token.get_secret_value(),
            ),
            timeout=20,
            headers={"Accept": "application/json"},
        )

    async def _throttle(self):
        # Conservative local pacing shared by clinic and outreach dispatch.
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self._next_request_at - loop.time()))
            self._next_request_at = loop.time() + 1.0

    async def dial(self, number, answer_url, callback_url, call_id):
        await self._throttle()
        logger.info(
            "Twilio dial started call_id=%s destination_suffix=%s answer_host=%s callback_host=%s",
            call_id,
            str(number)[-4:],
            urlsplit(answer_url).hostname,
            urlsplit(callback_url).hostname,
        )
        result = await self._request(
            "POST",
            "Calls.json",
            call_id=call_id,
            secrets=(
                number,
                self.settings.twilio_phone_number,
                answer_url,
                callback_url,
            ),
            data={
                "From": self.settings.twilio_phone_number,
                "To": number,
                "Url": answer_url,
                "Method": "POST",
                "StatusCallback": callback_url,
                "StatusCallbackMethod": "POST",
                "StatusCallbackEvent": [
                    "initiated",
                    "ringing",
                    "answered",
                    "completed",
                ],
                "TimeLimit": "1800",
                "Timeout": "60",
                "Record": "false",
            },
        )
        # Twilio returns the actual call SID immediately, unlike a request identifier.
        sid = result["sid"]
        if not isinstance(sid, str) or not sid:
            raise ValueError("Invalid Twilio call SID")
        status = call_status(result["status"])
        logger.info(
            "Twilio dispatch accepted call_id=%s sid=%s status=%s", call_id, sid, status
        )
        return {"Sid": sid, "Status": status}

    async def details(self, sid):
        await self._throttle()
        result = await self._request(
            "GET", f"Calls/{quote(sid, safe='')}.json", sid=sid
        )
        return {
            "Sid": result["sid"],
            "Status": call_status(result["status"]),
            "From": result["from"],
            "To": result["to"],
        }

    async def _request(
        self, method, path, *, call_id=None, sid=None, secrets=(), **kwargs
    ):
        started = time.monotonic()
        response = None
        secrets = (*secrets, self.settings.twilio_auth_token.get_secret_value())
        try:
            response = await self.http.request(method, path, **kwargs)
            response.raise_for_status()
            result = response.json()
            logger.info(
                "Twilio response method=%s call_id=%s sid=%s http_status=%s elapsed_ms=%.0f",
                method,
                call_id,
                sid,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            return result
        except Exception as exc:
            logger.error(
                "Twilio request failed method=%s call_id=%s sid=%s http_status=%s "
                "elapsed_ms=%.0f error_type=%s detail=%s response=%s",
                method,
                call_id,
                sid,
                response.status_code if response is not None else None,
                (time.monotonic() - started) * 1000,
                type(exc).__name__,
                safe_detail(exc, secrets),
                safe_detail(response.text, secrets)
                if response is not None
                else "unavailable",
            )
            raise

    async def close(self):
        await self.http.aclose()
