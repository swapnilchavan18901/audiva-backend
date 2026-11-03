"""HTTP admission limits and response metadata without touching voice WebSockets."""

import asyncio
import logging
import time
from uuid import uuid4

from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from app.utils.rate_limit import request_policy

logger = logging.getLogger(__name__)


async def validation_error(request, exc):
    """Do not reflect submitted passwords, keys, or context in validation errors."""
    errors = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse({"detail": errors}, status_code=422)


class HTTPProtectionMiddleware:
    def __init__(self, app, *, settings, limiter, skip_public=False):
        self.app, self.settings, self.limiter = app, settings, limiter
        self.skip_public = skip_public

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or (
            self.skip_public and path.startswith("/api/v1/public/")
        ):
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        response_started = False
        status = 500

        async def protected_send(message):
            nonlocal response_started, status
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["X-Frame-Options"] = "DENY"
                if path.startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
                if (
                    self.settings.app_environment == "production"
                    and scope.get("scheme") == "https"
                ):
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        async def reject(code, detail, headers=None):
            await JSONResponse(
                {"detail": detail, "request_id": request_id},
                status_code=code,
                headers=headers,
            )(scope, receive, protected_send)

        try:
            callback = path.startswith(
                ("/api/v1/clinic-phone/twilio/", "/api/v1/outreach/twilio/")
            )
            if (
                scope["method"] != "OPTIONS"
                and path not in {"/", "/health", "/ready"}
                and not callback
            ):
                # Uvicorn resolves client IPs only from explicitly trusted proxies.
                # Never trust X-Forwarded-For or X-Real-IP directly here.
                client = scope.get("client")
                identity = "ip:" + (client[0] if client else "unknown")
                policy = request_policy(scope["method"], path)
                self.limiter.check(policy, identity)
                if policy not in {"api", "cleanup"}:
                    self.limiter.check("api", identity)

            headers = Headers(scope=scope)
            body_limit = (
                min(self.settings.max_request_body_bytes, 16384)
                if callback
                else self.settings.max_request_body_bytes
            )
            content_length = headers.get("content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    await reject(400, "Invalid Content-Length header")
                    return
                if declared < 0:
                    await reject(400, "Invalid Content-Length header")
                    return
                if declared > body_limit:
                    await reject(413, "Request body is too large")
                    return

            # Bound actual bytes too: chunked requests can omit Content-Length.
            # Buffer before dispatch so oversized bodies never reach a write handler.
            if scope["method"] not in {"HEAD", "OPTIONS"}:
                body = bytearray()
                try:
                    async with asyncio.timeout(
                        self.settings.request_body_timeout_seconds
                    ):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                status = 499
                                return
                            body.extend(message.get("body", b""))
                            if len(body) > body_limit:
                                await reject(413, "Request body is too large")
                                return
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    await reject(408, "Request body took too long to arrive")
                    return
                if content_length is not None and len(body) != declared:
                    await reject(400, "Content-Length does not match the request body")
                    return
                replayed = False

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {
                            "type": "http.request",
                            "body": bytes(body),
                            "more_body": False,
                        }
                    return await receive()

                await self.app(scope, replay, protected_send)
            else:
                await self.app(scope, receive, protected_send)
        except HTTPException as exc:
            if response_started:
                raise
            await reject(exc.status_code, exc.detail, exc.headers)
        except Exception as exc:
            logger.error(
                "HTTP request failed request_id=%s error_type=%s",
                request_id,
                type(exc).__name__,
            )
            if response_started:
                raise
            await reject(
                500, "Something went wrong. Contact support with the request ID."
            )
        finally:
            # Do not log URLs, query strings, tokens, request bodies, or personal data.
            logger.info(
                "HTTP request request_id=%s method=%s status=%s duration_ms=%.0f",
                request_id,
                scope["method"],
                status,
                (time.monotonic() - started) * 1000,
            )
