"""Twilio answer XML, start-event validation, and shared mu-law audio transport."""

import json
import re
from xml.etree.ElementTree import Element, SubElement, tostring


def stream_xml(url):
    response = Element("Response")
    # Twilio does not support query strings on the Stream URL. Grants are in
    # the URL path and redacted from server logs by AudioGrantLogFilter.
    if "?" in url:
        raise ValueError("Twilio Stream URLs must not contain query parameters")
    connect = SubElement(response, "Connect")
    SubElement(connect, "Stream", {"url": url})
    SubElement(response, "Hangup")
    return tostring(response, encoding="unicode")


def validate_start(event, auth_id):
    start = event["start"]
    audio = start["mediaFormat"]
    if (
        event.get("event") != "start"
        or start.get("accountSid") != auth_id
        or not isinstance(start.get("callSid"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", start["callSid"])
        or not isinstance(start.get("streamSid"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", start["streamSid"])
        or audio.get("encoding") != "audio/x-mulaw"
        or int(audio["sampleRate"]) != 8000
        or int(audio.get("channels", 1)) != 1
    ):
        raise ValueError(
            "Invalid Twilio start event (expected account and 8kHz mu-law)"
        )
    return {
        "call_sid": start["callSid"],
        "stream_sid": start["streamSid"],
        "media_format": {"encoding": "audio/x-mulaw", "sample_rate": 8000},
    }


def build_transport(websocket, start):
    from pipecat.frames.frames import EndFrame
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    class Serializer(TwilioFrameSerializer):
        async def deserialize(self, data):
            if json.loads(data).get("event") == "stop":
                return EndFrame()
            return await super().deserialize(data)

    return FastAPIWebsocketTransport(
        websocket,
        FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=8000,
            audio_out_10ms_chunks=20,
            serializer=Serializer(
                start["stream_sid"],
                start["call_sid"],
                params=Serializer.InputParams(
                    twilio_sample_rate=8000, auto_hang_up=False
                ),
            ),
        ),
    )
