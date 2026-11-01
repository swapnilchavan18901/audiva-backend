"""Per-session Pipecat services: never share stateful processors between calls."""

from app.constants import constants


def create_sarvam_services(
    settings, language_code, *, system_prompt=None, text_filters=(), benchmark=None
):
    from pipecat.services.sarvam.llm import SarvamLLMService
    from pipecat.services.sarvam.stt import SarvamSTTService
    from pipecat.services.sarvam.tts import SarvamTTSService
    from pipecat.services.tts_service import TextAggregationMode
    from pipecat.transcriptions.language import Language

    if benchmark is not None:
        import asyncio

        class MeteredLLM(SarvamLLMService):
            async def get_chat_completions(self, context):
                request = benchmark.begin("llm", constants.SARVAM_LLM_MODEL)
                try:
                    stream = await super().get_chat_completions(context)
                except BaseException as exc:
                    benchmark.finish(
                        request,
                        status="interrupted"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed",
                    )
                    raise

                async def measured_stream():
                    usage = None
                    status = "completed"
                    try:
                        async for chunk in stream:
                            benchmark.first_chunk(request)
                            if chunk.id:
                                request["provider_request_id"] = chunk.id
                            if chunk.model:
                                request["model"] = chunk.model
                            for choice in chunk.choices or []:
                                delta = choice.delta
                                if delta and (
                                    delta.content
                                    or getattr(delta, "reasoning_content", None)
                                    or delta.tool_calls
                                ):
                                    benchmark.first_token(request)
                                if choice.finish_reason:
                                    request["finish_reason"] = choice.finish_reason
                            if chunk.usage:
                                usage = chunk.usage.model_dump()
                            yield chunk
                    except (asyncio.CancelledError, GeneratorExit):
                        status = "interrupted"
                        raise
                    except Exception:
                        status = "failed"
                        raise
                    finally:
                        benchmark.response_finished(request)
                        try:
                            await stream.close()
                        finally:
                            benchmark.finish(request, usage=usage, status=status)

                return measured_stream()

        SarvamLLMService = MeteredLLM

    key = settings.sarvam_api_key.get_secret_value()
    language = Language.OR_IN if language_code == "od-IN" else Language(language_code)
    llm_settings = {
        "model": constants.SARVAM_LLM_MODEL,
        # Sarvam requires explicit JSON null to disable thinking. Pipecat omits
        # its reasoning_effort=None setting, so pass it as a request override.
        "extra": {"reasoning_effort": None},
    }
    if system_prompt is not None:
        llm_settings["system_instruction"] = system_prompt
    stt = SarvamSTTService(
        api_key=key,
        settings=SarvamSTTService.Settings(
            model=constants.SARVAM_STT_MODEL, language=language
        ),
    )
    llm = SarvamLLMService(
        api_key=key,
        # V1 returns provider token usage in the final streaming chunk.
        base_url="https://api.sarvam.ai/v1",
        settings=SarvamLLMService.Settings(**llm_settings),
    )
    tts = SarvamTTSService(
        api_key=key,
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        text_filters=list(text_filters),
        settings=SarvamTTSService.Settings(
            model=constants.SARVAM_TTS_MODEL,
            voice=constants.SARVAM_VOICE,
            language=language,
        ),
    )
    return stt, llm, tts
