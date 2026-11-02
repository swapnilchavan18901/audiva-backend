"""Bounded parsing for signed phone answer and status callback endpoints."""

import json
from urllib.parse import parse_qs

from fastapi import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser


async def read_payload(request):
    if request.method == "GET":
        return dict(request.query_params)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 16384:
            raise HTTPException(413, "Callback too large")
    try:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            payload = json.loads(content)
        elif content_type.startswith("multipart/form-data"):

            async def stream():
                yield bytes(content)

            payload = dict(
                await MultiPartParser(
                    request.headers,
                    stream(),
                    max_files=0,
                    max_fields=20,
                    max_part_size=16384,
                ).parse()
            )
        else:
            payload = {k: v[-1] for k, v in parse_qs(content.decode()).items()}
        if not isinstance(payload, dict):
            raise TypeError
        return payload
    except (ValueError, AttributeError, TypeError, MultiPartException):
        raise HTTPException(400, "Invalid phone callback") from None
