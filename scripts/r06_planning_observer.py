"""Nonexecuting fresh-plan snapshots, never an actual/cached-plan claim.

Only owned test schemas are accepted. SQL, parameters, plan expressions and
prepared statement text are not exported. Metadata queries never auto-prepare.
"""

from datetime import datetime
import hashlib
import json
import math
import re
from time import perf_counter

from psycopg.rows import dict_row


PLAN_FIELDS = frozenset({"Node Type", "Parent Relationship", "Join Type", "Strategy",
    "Partial Mode", "Parallel Aware", "Async Capable", "Plan Rows", "Plan Width",
    "Startup Cost", "Total Cost", "Relation Name", "Index Name", "Scan Direction", "CTE Name", "Alias"})
ESTIMATE_FIELDS = frozenset({"Plan Rows", "Plan Width", "Startup Cost", "Total Cost"})


def sanitize_plan(raw):
    """Whitelist plan structure/estimates; omit constants and executable text."""
    budget = 1024
    def visit(node, depth=0):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 64 or type(node) is not dict or type(node.get("Node Type")) is not str:
            raise ValueError("invalid or oversized planning tree")
        result = {}
        for name in PLAN_FIELDS & node.keys():
            value = node[name]
            if type(value) not in (str, int, float, bool) or (
                type(value) is str and len(value) > 256
            ) or (type(value) is float and not math.isfinite(value)):
                raise ValueError("invalid planning scalar")
            result[name] = value
        if "Plans" in node:
            if type(node["Plans"]) is not list:
                raise ValueError("invalid plan children")
            result["Plans"] = [visit(child, depth+1) for child in node["Plans"]]
        return result
    if type(raw) is not list or len(raw) != 1 or type(raw[0]) is not dict or "Plan" not in raw[0]:
        raise ValueError("missing planning tree")
    return visit(raw[0]["Plan"])


def plan_fingerprint(plan, *, shape_only=False):
    def shape(node):
        return {name: [shape(child) for child in value] if name == "Plans" else value
                for name, value in node.items() if name not in ESTIMATE_FIELDS}
    value = shape(plan) if shape_only else plan
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _protocol_query(query, parameters):
    # The caller supplies an immutable internal SQL literal, not arbitrary SQL.
    # These literals use only unquoted %s placeholders and contain no other %.
    if type(query) is not str or type(parameters) is not tuple or len(query) > 100_000:
        raise ValueError("planning requires a bounded internal SQL literal and tuple")
    pieces = query.split("%s")
    if len(pieces)-1 != len(parameters) or any("%" in part for part in pieces):
        raise ValueError("unsupported internal SQL placeholder layout")
    return pieces[0]+"".join(f"${index}"+part for index, part in enumerate(pieces[1:], 1))


def read_planning_snapshot(connection, query, parameters):
    """Same session prepared counters, fresh non-ANALYZE EXPLAIN, scoped stats.

    These are sequential observations, not an atomic view of the measured SQL.
    EXPLAIN EXECUTE is intentionally avoided: it would choose a cached plan and
    increment its generic/custom counters even without executing the statement.
    """
    protocol = _protocol_query(query, parameters)
    if connection.closed or not connection.autocommit:
        raise ValueError("planning requires a live owned autocommit connection")
    started = perf_counter()
    with connection.cursor(row_factory=dict_row) as cursor:
        scope = cursor.execute("SELECT current_schema() AS schema, "
            "current_setting('plan_cache_mode') AS plan_cache_mode, "
            "current_setting('autovacuum') AS autovacuum, "
            "current_setting('autovacuum_analyze_threshold') AS analyze_threshold, "
            "current_setting('autovacuum_analyze_scale_factor') AS analyze_scale_factor, "
            "current_setting('autovacuum_naptime') AS autovacuum_naptime, "
            "current_setting('stats_fetch_consistency') AS stats_fetch_consistency", prepare=False).fetchone()
        if not re.fullmatch(r"test_evorec_[0-9a-f]{32}", scope["schema"] or ""):
            raise ValueError("planning refuses non-owned schema")
        tables = cursor.execute("SELECT c.relname, c.reltuples::double precision, c.relpages, c.relallvisible, "
            "c.reloptions, s.last_analyze, s.last_autoanalyze, s.analyze_count, s.autoanalyze_count, "
            "s.n_mod_since_analyze, s.n_live_tup, s.n_dead_tup "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_stat_all_tables s ON s.relid=c.oid "
            "WHERE n.nspname=%s AND c.relname IN ('items','bundle_items') ORDER BY c.relname",
            (scope["schema"],), prepare=False).fetchall()
        if [row["relname"] for row in tables] != ["bundle_items", "items"]:
            raise ValueError("owned catalog statistics unavailable")
        tables = [{name: value.isoformat() if isinstance(value, datetime) else value
                   for name, value in row.items()} for row in tables]
        prepared = cursor.execute("SELECT name, from_sql, generic_plans, custom_plans "
            "FROM pg_catalog.pg_prepared_statements WHERE statement=%s ORDER BY name",
            (protocol,), prepare=False).fetchall()
        raw = cursor.execute("EXPLAIN (FORMAT JSON) " + query, parameters, prepare=False).fetchone()["QUERY PLAN"]
    plan = sanitize_plan(raw)
    return dict(target_pid=connection.info.backend_pid, settings=scope, tables=tables, prepared=prepared,
        fresh_plan=plan, fresh_plan_sha256=plan_fingerprint(plan), shape_sha256=plan_fingerprint(plan, shape_only=True),
        observation_elapsed_ms=(perf_counter()-started)*1000,
        is_actual_measured_statement_plan=False, is_cached_prepared_plan=False,
        statistics_can_lag=True, metadata_auto_prepare=False, snapshots_are_not_atomic=True)
