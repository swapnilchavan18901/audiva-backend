"""Bounded, monotonic-clock token buckets for the single-worker voice server."""

import hashlib
import math
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class RatePolicy:
    capacity: int
    period: float = 60


class RateLimiter:
    def __init__(self, settings, *, clock=time.monotonic):
        self.clock = clock
        self.max_buckets = settings.rate_limit_max_buckets
        self.policies = {
            "api": RatePolicy(settings.api_requests_per_minute),
            "user": RatePolicy(settings.user_requests_per_minute),
            "login": RatePolicy(settings.login_requests_per_minute),
            "register": RatePolicy(settings.signup_requests_per_hour, 3600),
            "demo": RatePolicy(settings.demo_requests_per_hour, 3600),
            "write": RatePolicy(settings.write_requests_per_minute),
            "voice": RatePolicy(settings.voice_starts_per_minute),
            "phone": RatePolicy(settings.phone_calls_per_minute),
            "campaign": RatePolicy(settings.campaign_starts_per_minute),
            "grant": RatePolicy(settings.voice_tokens_per_minute),
            "cleanup": RatePolicy(60),
        }
        self.buckets = OrderedDict()
        self.next_cleanup = 0.0

    def check(self, policy_name, identity):
        """Consume one token without awaiting, making admission atomic per event loop.

        Store fixed-size digests, never bearer tokens, email addresses, or raw IPs.
        At capacity, reject new identities instead of evicting active limits.
        """
        now = self.clock()
        policy = self.policies[policy_name]
        key = (policy_name, hashlib.sha256(identity.encode()).digest())
        if now >= self.next_cleanup:
            expired = [
                key
                for key, (_, updated, period) in self.buckets.items()
                if now - updated >= period
            ]
            for old in expired:
                del self.buckets[old]
            self.next_cleanup = now + 60
        if key not in self.buckets and len(self.buckets) >= self.max_buckets:
            raise HTTPException(
                429,
                "Server is busy. Please try again shortly.",
                headers={"Retry-After": "60"},
            )
        tokens, updated, _ = self.buckets.get(
            key, (float(policy.capacity), now, policy.period)
        )
        tokens = min(
            policy.capacity,
            tokens + max(0, now - updated) * policy.capacity / policy.period,
        )
        self.buckets[key] = (tokens, now, policy.period)
        if tokens < 1:
            retry = max(1, math.ceil((1 - tokens) * policy.period / policy.capacity))
            raise HTTPException(
                429,
                "Too many requests. Please wait before trying again.",
                headers={"Retry-After": str(retry)},
            )
        self.buckets[key] = (tokens - 1, now, policy.period)


def request_policy(method, path):
    path = path.rstrip("/")
    if method == "DELETE" and ("/voice/" in path or "/webrtc/" in path):
        return "cleanup"
    if method == "POST":
        if path.endswith(("/leave", "/stop")):
            return "cleanup"
        for action in ("login", "register", "demo"):
            if path == f"/api/v1/auth/{action}":
                return action
        if path.endswith("/session-token"):
            return "grant"
        if path.endswith(("/voice/offer", "/webrtc/offer")):
            return "voice"
        if "/meetings/" in path and path.endswith("/join"):
            return "voice"
        if path.endswith("/phone/calls"):
            return "phone"
        if "/outreach/campaigns/" in path and path.endswith("/run"):
            return "campaign"
    return "write" if method in {"POST", "PUT", "PATCH", "DELETE"} else "api"


def enforce_rate_limit(request, policy, identity):
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        limiter.check(policy, identity)
