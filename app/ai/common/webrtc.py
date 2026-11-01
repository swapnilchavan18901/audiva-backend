"""Shared WebRTC audio wiring, with no domain-specific prompts or flow imports."""

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import (
    PipelineParams,
    PipelineWorker,
    ProcessorUnusablePolicy,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from app.ai.benchmark import Benchmark
from app.ai.common.benchmark import BenchmarkObserver
from app.clients import create_sarvam_services
from app.constants import constants
from app.utils.filler import ResponseFiller
from app.utils.text_normalizer import SpeechNormalizer


def build_webrtc(
    connection,
    bot,
    settings,
    *,
    messages=(),
    system_prompt=None,
    text_filters=(),
    input_processors=(),
):
    transport = SmallWebRTCTransport(
        connection,
        TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    return build_voice(
        transport,
        bot,
        settings,
        messages=messages,
        system_prompt=system_prompt,
        text_filters=text_filters,
        input_processors=input_processors,
    )


def build_voice(
    transport,
    bot,
    settings,
    *,
    messages=(),
    system_prompt=None,
    text_filters=(),
    input_processors=(),
    output_sample_rate=24000,
):
    """Use the same speech processing for browser and phone transports."""
    benchmark = Benchmark(getattr(bot, "service_type", "conversation"))
    stt, llm, tts = create_sarvam_services(
        settings,
        bot.language,
        system_prompt=system_prompt,
        text_filters=[SpeechNormalizer(bot.language), *text_filters],
        benchmark=benchmark,
    )
    aggregators = LLMContextAggregatorPair(
        LLMContext(messages=list(messages)),
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )
    user, assistant = aggregators
    worker = PipelineWorker(
        Pipeline(
            [
                transport.input(),
                stt,
                *input_processors,
                user,
                llm,
                ResponseFiller(bot.language, constants.FILLER_DELAY_MS),
                tts,
                transport.output(),
                assistant,
            ]
        ),
        app_resources={},
        observers=[BenchmarkObserver(benchmark)],
        params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=output_sample_rate,
        ),
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    runner = WorkerRunner(handle_sigint=False)

    @transport.event_handler("on_client_disconnected")
    async def disconnected(_transport, _client):
        await runner.cancel()

    return runner, worker, transport, llm, aggregators
