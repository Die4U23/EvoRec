"""Planner/stat snapshots cannot execute SQL or perturb prepared counters."""

import json
from types import SimpleNamespace

import psycopg
import pytest

from scripts.profile_r06_catalog_capture import _planning_observed, main, profile
from scripts.r06_planning_observer import (
    _protocol_query, plan_fingerprint, read_planning_snapshot, sanitize_plan,
)


def test_plan_projection_removes_expressions_and_parameters_recursively():
    raw = [{"Plan": {"Node Type": "Aggregate", "Total Cost": 3.5, "Output": ["secret password"],
        "Plans": [{"Node Type": "Seq Scan", "Relation Name": "items", "Plan Rows": 2,
                   "Filter": "id='PRIVATE_PARAMETER'", "Index Cond": "secret SQL"}]}}]
    result = sanitize_plan(raw)
    assert result == {"Node Type": "Aggregate", "Total Cost": 3.5,
        "Plans": [{"Node Type": "Seq Scan", "Relation Name": "items", "Plan Rows": 2}]}
    assert "secret" not in json.dumps(result) and "PRIVATE_PARAMETER" not in json.dumps(result)


@pytest.mark.parametrize("raw", [[], [{}, {}], [{"Plan": {}}],
    [{"Plan": {"Node Type": "Scan", "Plans": {}}}],
    [{"Plan": {"Node Type": "Scan", "Total Cost": float("nan")}}],
    [{"Plan": {"Node Type": "Scan", "Alias": "x"*257}}],
    [{"Plan": {"Node Type": "Root", "Plans": [{"Node Type": "Child"}]*1024}}]])
def test_invalid_or_oversized_plan_fails_closed(raw):
    with pytest.raises(ValueError):
        sanitize_plan(raw)


def test_deep_plan_is_bounded():
    plan = {"Node Type": "Leaf"}
    for _ in range(66):
        plan = {"Node Type": "Parent", "Plans": [plan]}
    with pytest.raises(ValueError):
        sanitize_plan([{"Plan": plan}])


def test_shape_and_estimate_fingerprints_are_distinct_stable_scopes():
    a = {"Node Type": "Scan", "Plan Rows": 1, "Total Cost": 2., "Plans": [{"Node Type": "Child", "Plan Width": 4}]}
    b = {"Plans": [{"Plan Width": 6, "Node Type": "Child"}], "Total Cost": 3., "Plan Rows": 2, "Node Type": "Scan"}
    assert plan_fingerprint(a) != plan_fingerprint(b)
    assert plan_fingerprint(a, shape_only=True) == plan_fingerprint(b, shape_only=True)
    assert plan_fingerprint(a) == plan_fingerprint(dict(reversed(list(a.items()))))
    b["Node Type"] = "Different"
    assert plan_fingerprint(a, shape_only=True) != plan_fingerprint(b, shape_only=True)


@pytest.mark.parametrize("query,parameters", [("SELECT %s", ()), ("SELECT 1 % 2", ()),
                                           (1, ()), ("SELECT %s", [1]), ("x"*100001, ())],
                         ids=["arity", "other-percent", "non-string", "non-tuple", "oversized"])
def test_unsupported_internal_literal_rejects_before_database(query, parameters):
    with pytest.raises(ValueError):
        read_planning_snapshot(None, query, parameters)


def test_protocol_text_preserves_sql_without_serializing_parameters():
    assert _protocol_query("SELECT %s::uuid, %s, %s", ("secret", 1, 2)) == "SELECT $1::uuid, $2, $3"


@pytest.mark.parametrize("closed,autocommit", [(True, True), (False, False)])
def test_planning_requires_live_autocommit_before_sql(closed, autocommit):
    with pytest.raises(ValueError, match="live owned autocommit"):
        read_planning_snapshot(SimpleNamespace(closed=closed, autocommit=autocommit), "SELECT 1", ())


def test_non_owned_schema_is_rejected_before_table_or_plan_queries():
    calls = []
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, query, **kwargs):
            calls.append(query)
            assert kwargs == {"prepare": False}
            return self
        def fetchone(self):
            return {"schema": "public"}
    connection = SimpleNamespace(closed=False, autocommit=True, cursor=lambda **kwargs: Cursor())
    with pytest.raises(ValueError, match="non-owned"):
        read_planning_snapshot(connection, "SELECT 1", ())
    assert len(calls) == 1 and "EXPLAIN" not in calls[0]


@pytest.mark.parametrize("cache_mode,counter", [("force_custom_plan", "custom_plans"),
                                               ("force_generic_plan", "generic_plans")])
def test_fresh_explain_does_not_increment_target_prepared_plan_choices(isolated_database, cache_mode, counter):
    with psycopg.connect(isolated_database, autocommit=True) as connection:
        # Any accidentally auto-prepared metadata is visible on its FIRST call;
        # three snapshots below the default threshold of five cannot prove this.
        connection.prepare_threshold = 0
        connection.execute(f"SET plan_cache_mode={cache_mode}", prepare=False)
        query, parameters = "SELECT count(*) FROM items WHERE item_id=%s", ("PRIVATE_PARAMETER",)
        connection.execute(query, parameters, prepare=True).fetchone()
        first = read_planning_snapshot(connection, query, parameters)
        second = read_planning_snapshot(connection, query, parameters)
        assert len(first["prepared"]) == 1 and first["prepared"] == second["prepared"]
        assert first["prepared"][0][counter] == 1
        assert not first["is_actual_measured_statement_plan"] and not first["is_cached_prepared_plan"]
        assert not first["metadata_auto_prepare"] and first["statistics_can_lag"]
        assert first["target_pid"] == connection.info.backend_pid
        assert "PRIVATE_PARAMETER" not in json.dumps(first) and "statement" not in first["prepared"][0]
        connection.execute(query, parameters, prepare=True).fetchone()
        third = read_planning_snapshot(connection, query, parameters)
        assert third["prepared"][0][counter] == 2
        # None of our four metadata queries created additional prepared statements.
        assert connection.execute("SELECT count(*) FROM pg_prepared_statements", prepare=False).fetchone() == (1,)


def test_removing_prepare_false_really_prepares_metadata_negative_control(isolated_database, monkeypatch):
    with psycopg.connect(isolated_database, autocommit=True) as connection:
        connection.prepare_threshold = 0
        query, parameters = "SELECT count(*) FROM items WHERE item_id=%s", ("private",)
        connection.execute(query, parameters, prepare=True).fetchone()
        original = psycopg.Cursor.execute
        def without_explicit_false(cursor, query, *args, **kwargs):
            if kwargs.get("prepare") is False:
                kwargs.pop("prepare")
            return original(cursor, query, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(psycopg.Cursor, "execute", without_explicit_false)
            read_planning_snapshot(connection, query, parameters)
        # The real driver/server register extra metadata statements: the positive
        # invariant of exactly one target statement above would fail here.
        assert connection.execute("SELECT count(*) FROM pg_prepared_statements", prepare=False).fetchone()[0] > 1


def test_non_analyze_explain_does_not_run_throwing_owned_function(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as connection:
        connection.execute("CREATE FUNCTION planner_probe() RETURNS integer LANGUAGE plpgsql VOLATILE AS $$ "
                           "BEGIN RAISE EXCEPTION 'must not execute'; END $$", prepare=False)
        snapshot = read_planning_snapshot(connection, "SELECT planner_probe()", ())
        assert snapshot["prepared"] == []
        assert snapshot["fresh_plan"]["Node Type"] == "Result"
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("SELECT planner_probe()", prepare=False)
        assert connection.execute("SELECT 1", prepare=False).fetchone() == (1,)


def test_analyze_catalog_estimates_change_without_forcing_stat_counter_timing(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as connection:
        connection.execute("INSERT INTO items(item_id,title,category,is_active) VALUES "
                           "('p1','Owned','test',true),('p2','Owned','test',true),('p3','Owned','test',true)", prepare=False)
        before = read_planning_snapshot(connection, "SELECT count(*) FROM items", ())
        connection.execute("ANALYZE items", prepare=False)
        after = read_planning_snapshot(connection, "SELECT count(*) FROM items", ())
        a = next(row for row in before["tables"] if row["relname"] == "items")
        b = next(row for row in after["tables"] if row["relname"] == "items")
        assert a["reltuples"] == -1 and b["reltuples"] == 3 and b["relpages"] > 0
        assert after["statistics_can_lag"] and after["snapshots_are_not_atomic"]
        assert after["fresh_plan_sha256"] == plan_fingerprint(after["fresh_plan"])
        assert before["fresh_plan_sha256"] != after["fresh_plan_sha256"]


def test_missing_owned_catalog_statistics_is_not_silently_successful(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as connection:
        connection.execute("ALTER TABLE bundle_items RENAME TO owned_renamed_probe", prepare=False)
        with pytest.raises(ValueError, match="statistics unavailable"):
            read_planning_snapshot(connection, "SELECT 1", ())


@pytest.mark.parametrize("options", [{"observe_plans": "yes"}, {"observe_plans": True}])
def test_invalid_option_or_missing_baseline_rejects_before_source(options):
    with pytest.raises(ValueError):
        profile(None, None, None, None, None, **options)


def test_planning_failure_preserves_sanitized_phase_without_retry(monkeypatch):
    calls, observation = [], {}
    def fail(*args):
        calls.append(1)
        raise ValueError("PRIVATE QUERY")
    monkeypatch.setattr("scripts.profile_r06_catalog_capture.read_planning_snapshot", fail)
    with pytest.raises(ValueError):
        _planning_observed(None, None, None, "before", observation)
    assert calls == [1] and observation == {"planning_error_type": "ValueError", "planning_error_phase": "before"}


@pytest.mark.parametrize("snapshots,count,expected", [(False,8,1), (True,0,1), (True,7,1), (True,8,0)])
def test_cli_rejects_missing_or_empty_planning_batch(monkeypatch, capsys, snapshots, count, expected):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE DSN")
    monkeypatch.setattr("sys.argv", ["probe", "out", "root", "00000000-0000-0000-0000-000000000000",
        "--expected-manifest-sha256", "f"*64, "--baseline-commit", "a"*40, "--observe-plans"])
    row = {f"planning_{phase}": dict(is_actual_measured_statement_plan=False)
           for phase in ("before", "after")} if snapshots else {}
    monkeypatch.setattr("scripts.profile_r06_catalog_capture.profile", lambda *args, **kwargs:
                        dict(item_count=1, measurements=[row]*count))
    assert main() == expected
    assert "PRIVATE DSN" not in capsys.readouterr().out
