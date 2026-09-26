"""Fixed, auditable starting histories for the local V0.1 experience."""


def initial_history(profile_id: str) -> tuple[str, ...]:
    if profile_id == "new":
        return ()
    if profile_id == "sample":
        return ("demo-coop",)
    raise ValueError("unknown session profile")
