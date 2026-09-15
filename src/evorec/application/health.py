from evorec.application.ports import ReadinessPort
from evorec.domain.models import ReadinessReport


class ReadinessQuery:
    def __init__(self, probe: ReadinessPort):
        self.probe = probe

    async def execute(self) -> ReadinessReport:
        return await self.probe.check()
