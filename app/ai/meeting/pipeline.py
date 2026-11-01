"""Per-speaker speech turns and explicitly addressed, interruptible replies."""

import asyncio
import base64
import binascii
import re
import time
from dataclasses import dataclass, field

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

from app.ai.meeting.prompts import scope_refusal
from app.constants import constants
from app.utils.filler import with_filler


@dataclass
class Speaker:
    vad: SileroVADAnalyzer
    audio: bytearray = field(default_factory=bytearray)
    preroll: bytes = b""
    active: bool = False
    parts: list[str] = field(default_factory=list)
    last_packet: float = 0.0
    benchmark_turn: int | None = None


class MeetingVoice:
    def __init__(
        self, websocket, capture, meeting, speech, save_turn, history, settings, get_memory=None
    ):
        self.websocket, self.capture, self.meeting = websocket, capture, meeting
        self.speech, self.save_turn, self.settings = speech, save_turn, settings
        self.get_memory = get_memory
        self.history = history
        self.speakers: dict[str, Speaker] = {}
        self.queue = asyncio.Queue(maxsize=64)
        self.phase = "listening"
        self.error = None
        self.answer_task = None
        self.generation = 0
        self.armed: dict[str, float] = {}
        name = re.escape(constants.MEETING_VOICE_NAME)
        self.address = re.compile(
            rf"^\s*(?:(?:hey|hi|hello|okay|ok)\s*[,!]?\s*)?{name}(?:\b|(?=[,!?]))[\s,:!?-]*(.*)$",
            re.I,
        )

    def interrupt(self):
        self.generation += 1
        if self.answer_task and not self.answer_task.done():
            self.answer_task.cancel()
        self.phase = "listening"

    async def receive_audio(self, message):
        if message.get("trigger") != "realtime_audio.per_participant":
            return
        if message.get("bot_id") != self.capture.provider_bot_id:
            raise ValueError("Unexpected assistant identity")
        data = message.get("data", {})
        participant = data.get("participant_uuid")
        if not isinstance(participant, str) or len(participant) > 200:
            raise ValueError("Missing participant identity")
        if data.get("sample_rate") != 16000:
            raise ValueError("Expected 16kHz PCM")
        try:
            audio = base64.b64decode(data.get("chunk", ""), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid audio chunk") from None
        if not audio or len(audio) % 2 or len(audio) > 32000:
            raise ValueError("Invalid PCM frame size")
        speaker = self.speakers.get(participant)
        if not speaker:
            if len(self.speakers) >= constants.MEETING_VOICE_MAX_PARTICIPANTS:
                raise ValueError("Participant capacity exceeded")
            vad = SileroVADAnalyzer(params=VADParams(start_secs=0.15, stop_secs=0.6))
            vad.set_sample_rate(16000)
            speaker = self.speakers[participant] = Speaker(vad)
        speaker.last_packet = time.monotonic()
        state = await speaker.vad.analyze_audio(audio)
        if state == VADState.SPEAKING and not speaker.active:
            speaker.active = True
            speaker.audio.extend(speaker.preroll)
            self.interrupt()
        if speaker.active:
            speaker.audio.extend(audio)
            final = state == VADState.QUIET
            if final or len(speaker.audio) >= 640000:
                if len(speaker.audio) >= 6400:
                    self.queue.put_nowait(
                        (participant, bytes(speaker.audio), final, self.generation)
                    )
                speaker.audio.clear()
                if final:
                    speaker.active = False
        speaker.preroll = (speaker.preroll + audio)[-9600:]

    async def flush_idle(self):
        # Some providers stop sending a participant's frames during silence.
        while True:
            await asyncio.sleep(0.1)
            for participant, speaker in self.speakers.items():
                if speaker.active and time.monotonic() - speaker.last_packet > 1:
                    if len(speaker.audio) >= 6400:
                        try:
                            self.queue.put_nowait(
                                (participant, bytes(speaker.audio), True, self.generation)
                            )
                        except asyncio.QueueFull:
                            self.error = "Speech processing is overloaded; a turn was skipped."
                    speaker.audio.clear()
                    speaker.active = False
                    speaker.preroll = b""

    async def consume(self):
        while True:
            participant, audio, final, generation = await self.queue.get()
            benchmark = getattr(self.speech, "benchmark", None)
            speaker = self.speakers[participant]
            if benchmark and speaker.benchmark_turn is None:
                speaker.benchmark_turn = benchmark.next_turn()
            token = benchmark.context_turn.set(speaker.benchmark_turn) if benchmark else None
            try:
                text = await self.speech.transcribe(audio) if audio else ""
                if text:
                    self.speakers[participant].parts.append(text)
                if not final:
                    continue
                text = " ".join(self.speakers[participant].parts)
                self.speakers[participant].parts.clear()
                if not text:
                    continue
                match = self.address.match(text)
                question = match.group(1).strip() if match else None
                if match and not question:
                    self.armed[participant] = time.monotonic() + 10
                elif not match and self.armed.pop(participant, 0) > time.monotonic():
                    question = text
                elif match:
                    self.armed.pop(participant, None)
                entry = await self.save_turn(participant, text, "user", question is not None)
                self.history.append(entry)
                self.history = self.history[-60:]
                # A newer human turn invalidates delayed STT/AI replies.
                if (
                    question
                    and generation == self.generation
                    and not any(speaker.active for speaker in self.speakers.values())
                ):
                    self.error = None
                    self.answer_task = asyncio.create_task(self.reply(question, generation))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = "A speech turn could not be processed. The assistant is still listening."
            finally:
                if benchmark:
                    benchmark.context_turn.reset(token)
                if final:
                    speaker.benchmark_turn = None
                self.queue.task_done()

    async def reply(self, question, generation):
        async def prepare_answer():
            memory = await self.get_memory() if self.get_memory else ""
            if not await self.speech.is_in_scope(question, self.meeting, self.history, memory):
                response = scope_refusal(constants.MEETING_VOICE_LANGUAGE)
            else:
                response = await self.speech.answer(
                    question, self.meeting, self.history, memory
                )
            return response, await self.speech.synthesize(response)

        async def speak_filler():
            try:
                audio = await self.speech.filler_audio()
                if generation == self.generation:
                    self.phase = "speaking"
                    await self.send_audio(audio, generation)
                    self.phase = "thinking"
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Filler failure must never discard the real answer.

        try:
            self.phase = "thinking"
            delay = constants.FILLER_DELAY_MS
            response, audio = await with_filler(prepare_answer(), delay, speak_filler)
            if generation != self.generation:
                return
            self.phase = "speaking"
            await self.send_audio(audio, generation)
            if generation != self.generation:
                return
            # Filler speech never enters this transcript or the answer context.
            entry = await self.save_turn(constants.MEETING_VOICE_NAME, response, "bot", False)
            self.history.append(entry)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = "The assistant could not finish its reply. It is still listening."
        finally:
            if generation == self.generation:
                self.phase = "listening"

    async def send_audio(self, audio, generation):
        if len(audio) % 2:
            raise ValueError("Output must be aligned PCM16")
        sample_rate = 16000
        bytes_per_second = sample_rate * 2  # Mono PCM16, not WAV container bytes.
        frame_bytes = bytes_per_second // 10  # 100 ms, reducing socket overhead.
        buffer_seconds = 0.2
        loop = asyncio.get_running_loop()
        started = loop.time()
        for offset in range(0, len(audio), frame_bytes):
            # Absolute deadlines prevent send overhead from accumulating into gaps.
            # A small lead absorbs socket/tunnel jitter without queuing a whole reply.
            deadline = started + offset / bytes_per_second - buffer_seconds
            delay = deadline - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if generation != self.generation:
                return
            await self.websocket.send_json(
                {
                    "trigger": "realtime_audio.bot_output",
                    "data": {
                        "chunk": base64.b64encode(audio[offset : offset + frame_bytes]).decode(),
                        "sample_rate": sample_rate,
                    },
                }
            )
        # Remain in the speaking state while the final buffered frames play out.
        remaining = started + len(audio) / bytes_per_second - loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def close(self):
        self.interrupt()
        if self.answer_task:
            await asyncio.gather(self.answer_task, return_exceptions=True)
        await asyncio.gather(*(speaker.vad.cleanup() for speaker in self.speakers.values()))
