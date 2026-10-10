"""Bounded R06 capture retains the old row implementation's decisions."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import pytest

from evorec.domain.errors import ManagementError
from evorec.infrastructure.r06_catalog_capture import capture_eligible, read_catalog_capture
from test_r06_online import _command, online


def _legacy_row_reference(connection, runtime):
    """Independent oracle transcribed from the pre-summary row predicate."""
    rows = connection.execute(
        "SELECT bi.item_id, bi.internal_item_id, i.is_active, i.r06_model_text, "
        "i.r06_first_seen_ms FROM bundle_items bi JOIN items i ON i.item_id=bi.item_id "
        "WHERE bi.bundle_id=%s ORDER BY bi.internal_item_id",
        (runtime.bundle_id,),
    ).fetchall()
    expected_ids = runtime.item_ids
    if (len(rows) != len(expected_ids)
            or any(row["item_id"] != expected_ids[index]
                   or row["internal_item_id"] != index
                   for index, row in enumerate(rows))):
        raise ManagementError("bundle_members_changed", "approved ordered membership changed", 409)

    eligible = set()
    for row in rows:
        if not row["is_active"]:
            continue
        item_id = row["item_id"]
        approved = runtime.catalog_items[item_id]
        text = row["r06_model_text"]
        if (text is None or row["r06_first_seen_ms"] is None
                or hashlib.sha256(text.encode("utf-8")).digest() != runtime.catalog_text_sha256[item_id]
                or row["r06_first_seen_ms"] != approved.first_seen_ms):
            raise ManagementError("r06_catalog_changed", "actual model text/time changed", 409)
        eligible.add(item_id)

    bundle = runtime.bundle
    seal_payload = [
        "r06-frozen-bundle-v1",
        bundle.manifest_sha256,
        sorted((item_id, bundle.catalog_item_sha256[item_id]) for item_id in eligible),
    ]
    seal = hashlib.sha256(json.dumps(
        seal_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return frozenset(eligible), seal


def _new_capture_result(connection, runtime):
    capture = read_catalog_capture(connection, runtime.bundle_id, len(runtime.item_ids))
    eligible = capture_eligible(runtime, capture)
    bundle = runtime.bundle
    payload = [
        "r06-frozen-bundle-v1",
        bundle.manifest_sha256,
        sorted((item_id, bundle.catalog_item_sha256[item_id]) for item_id in eligible),
    ]
    seal = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return eligible, seal


@pytest.mark.parametrize("scenario", [
    "full",
    "subset",
    "all_inactive",
    "append_extra",
    "prefix_extra",
    "delete_member",
    "replace_member_same_count",
    "swap_indices",
    "active_null_inputs",
    "active_text_drift",
    "active_time_drift",
    "inactive_drift",
    "reactivate_drifted_item",
])
def test_bounded_summary_matches_legacy_row_predicate_and_seal(online, scenario):
    app, identity, _ = online
    runtime = app.backend.runtime
    with app.backend._connect() as connection:
        if scenario == "subset":
            connection.execute("UPDATE items SET is_active=(item_id=ANY(%s))", (["a", "b", "c"],))
        elif scenario == "all_inactive":
            connection.execute("UPDATE items SET is_active=false")
        elif scenario in {"append_extra", "prefix_extra"}:
            connection.execute(
                "INSERT INTO items(item_id,title,category,is_active,r06_model_text) "
                "VALUES ('extra','Extra','test',true,NULL)"
            )
            index = 6 if scenario == "append_extra" else 0
            if scenario == "prefix_extra":
                connection.execute("UPDATE bundle_items SET internal_item_id=internal_item_id+100 "
                                   "WHERE bundle_id=%s", (identity,))
                connection.execute("UPDATE bundle_items SET internal_item_id=internal_item_id-99 "
                                   "WHERE bundle_id=%s", (identity,))
            connection.execute(
                "INSERT INTO bundle_items(bundle_id,item_id,internal_item_id) VALUES (%s,'extra',%s)",
                (identity, index),
            )
        elif scenario == "delete_member":
            connection.execute("DELETE FROM bundle_items WHERE bundle_id=%s AND item_id='a'", (identity,))
        elif scenario == "replace_member_same_count":
            connection.execute("INSERT INTO items(item_id,title,category) VALUES ('replacement','Replacement','test')")
            connection.execute("UPDATE bundle_items SET item_id='replacement' "
                               "WHERE bundle_id=%s AND item_id='a'", (identity,))
        elif scenario == "swap_indices":
            connection.execute("UPDATE bundle_items SET internal_item_id=100 "
                               "WHERE bundle_id=%s AND item_id='a'", (identity,))
            connection.execute("UPDATE bundle_items SET internal_item_id=0 "
                               "WHERE bundle_id=%s AND item_id='zero'", (identity,))
            connection.execute("UPDATE bundle_items SET internal_item_id=5 "
                               "WHERE bundle_id=%s AND item_id='a'", (identity,))
        elif scenario == "active_null_inputs":
            connection.execute("UPDATE items SET r06_model_text=NULL, r06_first_seen_ms=NULL "
                               "WHERE item_id='a'")
        elif scenario == "active_text_drift":
            connection.execute("UPDATE items SET r06_model_text='changed active text' WHERE item_id='a'")
        elif scenario == "active_time_drift":
            connection.execute("UPDATE items SET r06_first_seen_ms=99 WHERE item_id='a'")
        elif scenario == "inactive_drift":
            connection.execute("UPDATE items SET is_active=false, r06_model_text='inactive drift', "
                               "r06_first_seen_ms=99 WHERE item_id='a'")
        elif scenario == "reactivate_drifted_item":
            connection.execute("UPDATE items SET is_active=false, r06_model_text='inactive drift', "
                               "r06_first_seen_ms=99 WHERE item_id='a'")
            connection.execute("UPDATE items SET is_active=true WHERE item_id='a'")

        try:
            expected = _legacy_row_reference(connection, runtime)
            legacy_error = None
        except ManagementError as error:
            expected, legacy_error = None, error.code

        try:
            actual = _new_capture_result(connection, runtime)
            actual_error = None
        except ManagementError as error:
            actual, actual_error = None, error.code

    assert actual_error == legacy_error
    if legacy_error is None:
        assert actual == expected
        expected_eligible, expected_seal = expected

        async def verify_admission():
            context = await app.backend.snapshot_for_comparison(await _command(app))
            assert context.catalog.eligible_items == expected_eligible
            assert context.model.catalog_sha256 == expected_seal

        asyncio.run(verify_admission())


@pytest.mark.parametrize("placement", ["prefix", "suffix", "many_prefix", "many_suffix"])
def test_overbound_members_are_limited_before_content_capture(online, placement):
    app, identity, _ = online
    runtime = app.backend.runtime
    approved_count = len(runtime.item_ids)
    many = placement.startswith("many_")
    prefix = placement.endswith("prefix")
    extra_count = approved_count + 17 if many else 1
    extra_ids = [f"extra{index}" for index in range(extra_count)]

    with app.backend._connect() as connection:
        if prefix:
            # Avoid transient collisions with the immediate UNIQUE index.
            shift = approved_count + extra_count + 1
            connection.execute("UPDATE bundle_items SET internal_item_id=internal_item_id+%s "
                               "WHERE bundle_id=%s", (shift, identity))
            connection.execute("UPDATE bundle_items SET internal_item_id=internal_item_id-%s "
                               "WHERE bundle_id=%s", (shift - extra_count, identity))
        for index, item_id in enumerate(extra_ids):
            connection.execute(
                "INSERT INTO items(item_id,title,category,is_active,r06_model_text) "
                "VALUES (%s,%s,'test',true,NULL)", (item_id, item_id),
            )
            member_index = index if prefix else approved_count + index
            connection.execute("INSERT INTO bundle_items(bundle_id,item_id,internal_item_id) "
                               "VALUES (%s,%s,%s)", (identity, item_id, member_index))

        capture = read_catalog_capture(connection, identity, approved_count)
        assert capture.member_count == approved_count + 1
        assert capture.active_count == 0
        assert capture.invalid_active_count == 0
        assert capture.active_sha256 == hashlib.sha256(b"").digest()
        with pytest.raises(ManagementError) as error:
            capture_eligible(runtime, capture)
        assert error.value.code == "bundle_members_changed"


def test_summary_remains_one_statement_snapshot_during_concurrent_change(online):
    app, _, _ = online
    runtime = app.backend.runtime
    changed = False

    class CursorProxy:
        def __init__(self, owner, cursor):
            self.owner = owner
            self.cursor = cursor

        def __enter__(self):
            self.cursor.__enter__()
            return self

        def __exit__(self, *exc):
            return self.cursor.__exit__(*exc)

        def execute(self, *args, **kwargs):
            self.owner.statements += 1
            self.cursor.execute(*args, **kwargs)
            return self

        def fetchone(self):
            nonlocal changed
            row = self.cursor.fetchone()
            if not changed:
                changed = True
                with app.backend._connect() as writer:
                    writer.execute("UPDATE items SET is_active=false WHERE item_id='a'")
            return row

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection
            self.statements = 0

        def cursor(self, **kwargs):
            return CursorProxy(self, self.connection.cursor(**kwargs))

    with app.backend._connect() as connection:
        proxy = ConnectionProxy(connection)
        capture = read_catalog_capture(proxy, runtime.bundle_id, len(runtime.item_ids))
        assert proxy.statements == 1
        assert "a" in capture_eligible(runtime, capture)

    with app.backend._connect() as connection:
        current = read_catalog_capture(connection, runtime.bundle_id, len(runtime.item_ids))
        assert "a" not in capture_eligible(runtime, current)


def test_statement_snapshot_survives_change_while_sql_hashing_is_blocked(online):
    """Actual writer commits during SELECT, not just after fetching its summary."""
    from evorec.infrastructure.r06_catalog_capture import CATALOG_CAPTURE_SQL
    app, _, _ = online
    runtime = app.backend.runtime
    key = uuid4().int & ((1 << 63) - 1)
    with app.backend._connect() as connection:
        connection.execute(f"""
            CREATE FUNCTION test_capture_text(value text, item text) RETURNS text
            LANGUAGE plpgsql AS $function$
            BEGIN
                IF item = 'a' THEN
                    PERFORM pg_advisory_lock({key});
                    PERFORM pg_advisory_unlock({key});
                END IF;
                RETURN value;
            END;
            $function$
        """)
    gated_sql = CATALOG_CAPTURE_SQL.replace(
        "pg_catalog.convert_to(r06_model_text, 'UTF8')",
        "pg_catalog.convert_to(test_capture_text(r06_model_text, item_id), 'UTF8')",
    )
    assert gated_sql != CATALOG_CAPTURE_SQL
    pid = Queue(maxsize=1)

    def capture_in_thread():
        from evorec.infrastructure.r06_catalog_capture import CatalogCapture
        with app.backend._connect() as connection:
            pid.put(connection.info.backend_pid)
            row = connection.execute(gated_sql, (runtime.bundle_id, len(runtime.item_ids) + 1,
                                                 len(runtime.item_ids))).fetchone()
            row['inactive_ids'] = tuple(row['inactive_ids'])
            return CatalogCapture(**row)

    with app.backend._connect() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.autocommit = True
        blocker.execute("SELECT pg_advisory_lock(%s)", (key,))
        future = pool.submit(capture_in_thread)
        try:
            backend_pid = pid.get(timeout=10)
            until = monotonic() + 10
            while not blocker.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s "
                "AND locktype='advisory' AND NOT granted) AS waiting", (backend_pid,),
            ).fetchone()['waiting']:
                assert monotonic() < until, "SQL capture never reached the held test gate"
                sleep(.01)
            with app.backend._connect() as writer:
                writer.execute("UPDATE items SET is_active=false, r06_model_text='concurrent drift', "
                               "r06_first_seen_ms=99 WHERE item_id='c'")
        finally:
            blocker.execute("SELECT pg_advisory_unlock(%s)", (key,))
        capture = future.result(timeout=10)
    assert capture_eligible(runtime, capture) == runtime.full_eligible_items
    with app.backend._connect() as connection:
        current = read_catalog_capture(connection, runtime.bundle_id, len(runtime.item_ids))
    assert capture_eligible(runtime, current) == runtime.full_eligible_items - {'c'}


@pytest.mark.parametrize("scenario", ["overflow", "all_inactive"])
def test_narrow_frames_do_not_evaluate_text_outside_active_approved_count(online, scenario):
    """A throwing function proves skipped text work, not just a zero count."""
    from evorec.infrastructure.r06_catalog_capture import CATALOG_CAPTURE_SQL, CatalogCapture
    from psycopg.errors import RaiseException
    app, identity, _ = online
    runtime = app.backend.runtime
    count = len(runtime.item_ids)
    with app.backend._connect() as connection:
        connection.execute("""
            CREATE FUNCTION test_forbidden_text(value text) RETURNS text
            LANGUAGE plpgsql AS $function$
            BEGIN
                RAISE EXCEPTION 'text hashing must not run';
            END;
            $function$
        """)
        if scenario == "overflow":
            connection.execute("INSERT INTO items(item_id,title,category,is_active) "
                               "VALUES ('extra','Extra','test',true)")
            connection.execute("INSERT INTO bundle_items(bundle_id,item_id,internal_item_id) "
                               "VALUES (%s,'extra',%s)", (identity, count))
        else:
            connection.execute("UPDATE items SET is_active=false")
        gated_sql = CATALOG_CAPTURE_SQL.replace(
            "pg_catalog.convert_to(r06_model_text, 'UTF8')",
            "pg_catalog.convert_to(test_forbidden_text(r06_model_text), 'UTF8')",
        )
        assert gated_sql != CATALOG_CAPTURE_SQL
        row = connection.execute(gated_sql, (identity, count+1, count)).fetchone()
        row["inactive_ids"] = tuple(row["inactive_ids"])
        capture = CatalogCapture(**row)
        assert capture.active_count == capture.invalid_active_count == 0
        if scenario == "overflow":
            with pytest.raises(ManagementError, match="approved ordered membership changed"):
                capture_eligible(runtime, capture)
        else:
            assert capture_eligible(runtime, capture) == frozenset()
        # Positive control: the identical instrumented SQL must reach the
        # function once membership and an active item make hashing applicable.
        if scenario == "overflow":
            connection.execute("DELETE FROM bundle_items WHERE bundle_id=%s AND item_id='extra'",
                               (identity,))
        else:
            connection.execute("UPDATE items SET is_active=true WHERE item_id='a'")
        with pytest.raises(RaiseException, match="text hashing must not run"):
            with connection.transaction():
                connection.execute(gated_sql, (identity, count+1, count)).fetchone()
