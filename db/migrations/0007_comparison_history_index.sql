-- Paginate session-owned history without scanning other sessions' saved results.
CREATE INDEX strategy_comparisons_session_history
    ON strategy_comparisons(session_id, created_at DESC, comparison_id DESC);
