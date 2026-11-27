"""Bridge synchronous Runner calls onto Harbor's event loop and block new calls once cancelled."""

import asyncio
from threading import Event

from multi_round.models import ExternalExecutionError


class EnvironmentBridge:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.cancelled = Event()

    def check(self):
        if self.cancelled.is_set():
            raise ExternalExecutionError("Harbor Trial cancelled")

    def call(self, operation, *args, **kwargs):
        self.check()
        return asyncio.run_coroutine_threadsafe(
            operation(*args, **kwargs), self.loop
        ).result()


class GuardedAgent:
    def __init__(self, agent, bridge: EnvironmentBridge):
        self.agent, self.bridge = agent, bridge

    def run(self, call):
        self.bridge.check()
        return self.agent.run(call)
