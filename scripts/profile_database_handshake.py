"""Read-only loopback connection comparisons, not a production fix or SLA."""

import argparse
import ipaddress
from itertools import permutations
import json
import os
from pathlib import Path
import platform
from statistics import median
from time import perf_counter, thread_time

import psycopg
from psycopg.conninfo import conninfo_to_dict

from scripts.assemble_r06_bundle import _source
from scripts.run_r06_demo import marker
from scripts.verify_r06_reliability import percentiles, subprocess_sources


ARMS = ("baseline", "gss_disabled", "hostaddr_explicit")
ORDERS = tuple(permutations(ARMS))
METADATA = """SELECT pg_catalog.host(pg_catalog.inet_server_addr()),
    pg_catalog.inet_server_port(), pg_catalog.current_database(),
    current_user, pg_catalog.current_setting('transaction_read_only'),
    gss_authenticated, encrypted FROM pg_catalog.pg_stat_gssapi
    WHERE pid=pg_catalog.pg_backend_pid()"""


class GuardError(ValueError):
    """Fixed local codes only; never export driver exception messages."""


def parameters(database_url):
    if any(name.upper().startswith("PG") for name in os.environ):
        raise GuardError("inherited_pg_parameters_refused")
    try:
        params = conninfo_to_dict(database_url)
        address = ipaddress.ip_address(params.get("host", ""))
        if not address.is_loopback or params.get("service") or params.get("servicefile"):
            raise ValueError
        if params.get("hostaddr") and ipaddress.ip_address(params["hostaddr"]) != address:
            raise ValueError
        port = params.get("port", "5432")
        if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError
        if "=" in params.get("dbname", "") or params.get("dbname", "").startswith(("postgres:", "postgresql:")):
            raise ValueError
        if params.get("gssencmode", "prefer").lower() not in ("prefer", "disable"):
            raise ValueError
        if any(auth in params.get("require_auth", "").lower() for auth in ("gss", "sspi")):
            raise ValueError
        if params.get("replication", "false").lower() not in ("false", "off", "no", "0"):
            raise ValueError
        timeout = int(params.get("connect_timeout", "5"))
    except (ValueError, psycopg.Error):
        raise GuardError("unsafe_or_invalid_connection_parameters") from None
    # Identical diagnostic-only bounds in all arms. SSL/auth policy untouched.
    params["connect_timeout"] = str(min(timeout, 5) if timeout > 0 else 5)
    params["options"] = params.get("options", "") + " -c default_transaction_read_only=on -c statement_timeout=2000"
    return {"baseline": params,
            "gss_disabled": {**params, "gssencmode": "disable"},
            "hostaddr_explicit": {**params, "hostaddr": params["host"]}}


def observe(params, arm, round_index, expected=None):
    record = dict(arm=arm, round=round_index, warmup=round_index == -1, success=False,
                  error_code=None, connect_ms=None, connect_thread_cpu_ms=None,
                  metadata_ms=None, close_ms=None, connection_closed=None)
    connection, identity = None, None
    started, cpu_started = perf_counter(), thread_time()
    try:
        connection = psycopg.connect(**params, autocommit=True)
        record.update(connect_ms=1000*(perf_counter()-started),
                      connect_thread_cpu_ms=1000*(thread_time()-cpu_started))
        metadata_started = perf_counter()
        row = connection.execute(METADATA).fetchone()
        record["metadata_ms"] = 1000*(perf_counter()-metadata_started)
        if (not row or not ipaddress.ip_address(row[0]).is_loopback or row[4] != "on"
                or row[5] is not False or row[6] is not False):
            raise GuardError("nonlocal_writable_or_gss_session_refused")
        # Internal identity only: no host, database, user or their hashes exported.
        identity = tuple(row[:4]) + (connection.info.server_version, connection.pgconn.ssl_in_use)
        if expected is not None and identity != expected:
            raise GuardError("connection_identity_or_ssl_changed")
        record.update(success=True, server_version=connection.info.server_version,
                      ssl_in_use=connection.pgconn.ssl_in_use, gss_authenticated=False,
                      gss_encrypted=False, endpoint_is_loopback=True, read_only=True)
    except GuardError as error:
        record["error_code"] = str(error)
    except Exception:
        record["error_code"] = "connection_or_metadata_error"
    finally:
        if record["connect_ms"] is None:
            record["connect_ms"] = 1000*(perf_counter()-started)
            record["connect_thread_cpu_ms"] = 1000*(thread_time()-cpu_started)
        close_started = perf_counter()
        if connection is not None:
            try:
                connection.close()
                record["connection_closed"] = bool(connection.closed)
                if not connection.closed:
                    raise RuntimeError
            except Exception:
                record.update(success=False, error_code="connection_close_failed")
        record["close_ms"] = 1000*(perf_counter()-close_started)
    return record, identity


def summarize(records, rounds):
    load = [record for record in records if not record["warmup"]]
    result = {}
    for arm in ARMS:
        selected = [r for r in load if r["arm"] == arm]
        successful = [r for r in selected if r["success"]]
        values = [r["connect_ms"] for r in successful]
        result[arm] = dict(planned=rounds, observed=len(selected), successful=len(successful),
            failures=len(selected)-len(successful), missing=rounds-len(selected),
            connect_median_ms=median(values) if values else None,
            connect_percentiles_ms=percentiles(values),
            connect_thread_cpu_median_ms=median([r["connect_thread_cpu_ms"] for r in successful]) if values else None)
    paired = {}
    for arm in ARMS[1:]:
        differences = []
        for index in range(rounds):
            group = {r["arm"]: r for r in load if r["round"] == index and r["success"]}
            differences.append(group[arm]["connect_ms"]-group["baseline"]["connect_ms"]
                               if arm in group and "baseline" in group else None)
        values = [value for value in differences if value is not None]
        paired[arm] = dict(candidate_minus_baseline_ms=differences, pairs=len(values),
                          median_delta_ms=median(values) if values else None)
    return result, paired


def profile(output, database_url, *, rounds=12):
    if type(rounds) is not int or not 6 <= rounds <= 24 or rounds % 6:
        raise GuardError("rounds_must_be_6_12_18_or_24")
    arms = parameters(database_url)
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if (output == project / "artifacts" or not output.is_relative_to(project / "artifacts")
            or output.exists()):
        raise GuardError("fresh_artifacts_subdirectory_required")
    commit, hashes = _source(project), subprocess_sources(project)
    output.mkdir(parents=True, exist_ok=False)
    records, expected, stopped = [], None, None
    started = perf_counter()
    schedule = [(-1, ARMS)] + [(index, ORDERS[index % 6]) for index in range(rounds)]
    for index, order in schedule:
        for arm in order:
            if perf_counter()-started >= 60:
                stopped = "probe_budget_exhausted"
                break
            record, identity = observe(arms[arm], arm, index, expected)
            records.append(record)
            if expected is None:
                expected = identity
            if ((index == -1 and not record["success"])
                    or record["error_code"] in ("connection_identity_or_ssl_changed",
                        "nonlocal_writable_or_gss_session_refused", "connection_close_failed")):
                stopped = "safety_or_warmup_gate_failed"
                break
        if stopped:
            break
    summary, paired = summarize(records, rounds)
    complete = len(records) == 3*(rounds+1) and all(r["success"] for r in records) and stopped is None
    try:
        source_unchanged = _source(project) == commit and subprocess_sources(project) == hashes
    except Exception:
        source_unchanged = False
    result = dict(status="connection_diagnostic_complete_not_performance_acceptance" if complete and source_unchanged else "incomplete_or_failed_connection_diagnostic",
        source_commit=commit, source_sha256=hashes, source_unchanged=source_unchanged,
        driver_version=psycopg.__version__, libpq_version=psycopg.pq.version(),
        python_version=platform.python_version(), platform=platform.system(),
        rounds=rounds, warmup_planned=3, scheduled_orders=[list(order) for _, order in schedule],
        observations=records, summary=summary, paired_comparisons=paired,
        stop_code=stopped, complete=complete and source_unchanged,
        connect_timeout_cap_seconds=5, statement_timeout_ms=2000,
        scheduling_budget_seconds=60, in_flight_call_can_outlive_scheduling_budget=True,
        automatic_retries=0, physical_connection_per_observation=True,
        metadata_and_close_excluded_from_connect_time=True,
        thread_cpu_is_client_thread_only_not_server_cpu=True,
        numeric_loopback_host_no_hostname_resolution_trial=True,
        identical_readonly_and_timeout_options_added_to_all_arms=True,
        ssl_policy_unchanged=True, production_configuration_changed=False,
        sql_business_data_or_credentials_exported=False,
        schema_or_business_data_writes=False, schema_created=False,
        serial_connection_trial_not_concurrent_http_load=True,
        controlled_connection_trial_not_statistical_or_sla_proof=True, sla_proven=False)
    marker(output, "profile", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--rounds", type=int, default=12)
    args = parser.parse_args(argv)
    try:
        result = profile(args.output, os.environ["EVOREC_DATABASE_URL"], rounds=args.rounds)
    except Exception:
        print(json.dumps(dict(status="connection_diagnostic_failed", error_code="preflight_or_report_failure")))
        return 1
    print(json.dumps(dict(status=result["status"], complete=result["complete"], output=str(args.output))))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
