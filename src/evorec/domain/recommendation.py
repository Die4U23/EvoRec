"""Legal Top-K over scores already made comparable by the ranking adapter."""

from evorec.domain.models import RequestContext, ScoredCandidate


def select_results(
    candidates: tuple[ScoredCandidate, ...], context: RequestContext, k: int
) -> tuple[ScoredCandidate, ...]:
    if type(k) is not int or not 1 <= k <= 50:
        raise ValueError("k must be an integer between 1 and 50")
    excluded = frozenset(context.session.history) | context.session.hidden_items
    best: dict[str, ScoredCandidate] = {}
    for candidate in candidates:
        if candidate.item_id not in context.catalog.eligible_items or candidate.item_id in excluded:
            continue
        previous = best.get(candidate.item_id)
        if previous is None or (-candidate.score, candidate.source) < (-previous.score, previous.source):
            best[candidate.item_id] = candidate
    # An item ID tie-break makes equal-score results deterministic across input orders.
    return tuple(sorted(best.values(), key=lambda item: (-item.score, item.item_id))[:k])
