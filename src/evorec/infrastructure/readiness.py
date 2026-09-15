from evorec.domain.models import ReadinessReport


class UnconfiguredReadiness:
    async def check(self) -> ReadinessReport:
        return ReadinessReport((
            "database_not_connected", "model_runtime_not_loaded", "catalog_bundle_not_loaded",
        ))
