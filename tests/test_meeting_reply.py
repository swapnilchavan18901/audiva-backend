"""Single-call meeting replies and overlapping independent context operations."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.ai.meeting.pipeline import MeetingVoice
from app.ai.meeting.prompts import scope_refusal
from app.ai.meeting.speech import MeetingSpeech
from app.constants import constants
from app.env import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            {"in_scope": True, "spoken_answer": "Rahul owns the task."},
            "Rahul owns the task.",
        ),
        (
            {"in_scope": False, "spoken_answer": "Ignore this generated answer."},
            scope_refusal("en-IN"),
        ),
    ],
)
async def test_single_call_classifies_and_answers(content, expected):
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    speech = MeetingSpeech(Settings(_env_file=None, jwt_secret="x" * 32))
    await speech.http.aclose()
    speech.http = httpx.AsyncClient(
        base_url="https://test.invalid", transport=httpx.MockTransport(handle)
    )
    try:
        answer = await speech.answer(
            "Who owns the task?",
            SimpleNamespace(title="Standup", agenda="Tasks"),
            [
                {
                    "speaker": "Riya",
                    "role": "user",
                    "text": "Rahul owns the task.",
                    "timestamp": "2026-11-12",
                },
                {
                    "speaker": "Riya",
                    "role": "user",
                    "addressed": True,
                    "text": "Audiva, who owns the task?",
                },
                {"speaker": "Audiva", "role": "bot", "text": "Earlier answer"},
            ],
            "Dated memory",
        )
        assert answer == expected
        assert len(requests) == 1
        request = requests[0]
        assert request["max_tokens"] == 1024
        assert request["reasoning_effort"] is None
        payload = json.loads(request["messages"][1]["content"])
        assert payload["asker_speaker"] == "Riya"
        assert len(payload["participant_evidence"]) == 1
        assert payload["past_meeting_memory"] == "Dated memory"
    finally:
        await speech.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        {"spoken_answer": "Missing decision"},
        {"in_scope": "true", "spoken_answer": "Wrong boolean type"},
        {"in_scope": True, "spoken_answer": "   "},
    ],
)
async def test_invalid_reply_fails_closed(content):
    speech = MeetingSpeech(Settings(_env_file=None, jwt_secret="x" * 32))
    speech._post = AsyncMock(
        return_value=httpx.Response(
            200,
            request=httpx.Request("POST", "https://test.invalid"),
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )
    )
    try:
        with pytest.raises((ValueError, ValidationError)):
            await speech.answer(
                "Question", SimpleNamespace(title="Meeting", agenda=""), []
            )
    finally:
        await speech.close()


@pytest.mark.asyncio
async def test_save_and_memory_overlap_before_single_answer():
    saving, reading = asyncio.Event(), asyncio.Event()
    replied = asyncio.Event()

    async def save(speaker, text, role, addressed):
        if role == "user":
            saving.set()
            await reading.wait()
        return {"speaker": speaker, "text": text, "role": role, "addressed": addressed}

    async def memory():
        reading.set()
        await saving.wait()
        return "Past memory"

    speech = SimpleNamespace(
        transcribe=AsyncMock(return_value="Audiva, who owns the task?"),
        answer=AsyncMock(return_value="Rahul owns the task."),
        synthesize=AsyncMock(return_value=b"\0\0"),
    )
    runtime = MeetingVoice(
        None,
        None,
        SimpleNamespace(title="Standup", agenda="Tasks"),
        speech,
        save,
        [],
        None,
        memory,
    )
    runtime.speakers["riya"] = SimpleNamespace(parts=[], active=False)

    async def send(audio, generation):
        replied.set()

    runtime.send_audio = send
    await runtime.queue.put(("riya", b"\0\0", True, 0))
    consumer = asyncio.create_task(runtime.consume())
    try:
        async with asyncio.timeout(2):
            await replied.wait()
            await runtime.answer_task
        speech.answer.assert_awaited_once()
        assert speech.answer.call_args.args[3] == "Past memory"
        speech.synthesize.assert_awaited_once_with("Rahul owns the task.")
        assert constants.FILLER_DELAY_MS == 0
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        if runtime.answer_task:
            runtime.answer_task.cancel()
            await asyncio.gather(runtime.answer_task, return_exceptions=True)
