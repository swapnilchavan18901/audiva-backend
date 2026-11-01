"""Turn markers for the shared Pipecat pipelines."""

from pipecat.frames.frames import UserStartedSpeakingFrame
from pipecat.observers.base_observer import BaseObserver


class BenchmarkObserver(BaseObserver):
    def __init__(self, benchmark):
        super().__init__(observe_every_push=False)
        self.benchmark = benchmark

    async def on_push_frame(self, data):
        if isinstance(data.frame, UserStartedSpeakingFrame):
            self.benchmark.next_turn()
