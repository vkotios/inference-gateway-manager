import asyncio
import datetime
import json

import pytest

from gateway.admission import (
    BATCH,
    INTERACTIVE,
    Admission,
    AdmissionRefused,
    Capacity,
    reserved_at,
    resolve_settings,
    validate_settings,
)

UTC = datetime.timezone.utc


def make(total=3, layers=(), native=None, **kw):
    adm = Admission(**kw)
    adm.configure("eng/model", list(layers), Capacity(total, native))
    return adm


async def queued(adm, count):
    """Wait until ``count`` requests are in the queue.

    The tests wait on the broker's own state, never on a number of event-loop turns: how many
    turns it takes a task to resume after a future resolves differs between Python versions
    (``wait_for`` was rewritten in 3.12), and the broker resolves futures synchronously.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + 2
    while len(adm.queue) != count:
        assert loop.time() < end, f"expected {count} queued, have {[e.id for e in adm.queue]}"
        await asyncio.sleep(0)


async def done(task):
    """The task's result, waiting for it to complete (bounded)."""
    return await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_single_slot_load_keeps_the_strict_refusal():
    adm = make(total=1)
    assert adm.strict()
    first = await adm.acquire("a", BATCH, "c")
    with pytest.raises(AdmissionRefused) as err:
        await adm.acquire("b", INTERACTIVE, "c")
    assert err.value.status == 409
    assert "chat slot is free" in err.value.detail
    adm.release(first)
    await adm.acquire("b", INTERACTIVE, "c")


@pytest.mark.asyncio
async def test_single_mode_forces_refusal_even_with_several_slots():
    adm = make(total=3, layers=[{"mode": "single"}])
    grants = [await adm.acquire(str(i), BATCH, "c") for i in range(3)]
    with pytest.raises(AdmissionRefused) as err:
        await adm.acquire("x", INTERACTIVE, "c")
    assert err.value.status == 409 and not adm.queue
    assert len(grants) == 3


@pytest.mark.asyncio
async def test_extra_requests_queue_fifo_and_start_on_release():
    adm = make(total=2)
    a = await adm.acquire("a", BATCH, "c")
    await adm.acquire("b", BATCH, "c")
    waiters = [asyncio.create_task(adm.acquire(n, BATCH, "c")) for n in ("c", "d")]
    await queued(adm, 2)
    assert [e.id for e in adm.queue] == ["c", "d"]
    adm.release(a)
    first = await done(waiters[0])
    assert "c" in adm.running and "d" not in adm.running
    assert [e.id for e in adm.queue] == ["d"]
    assert first.waited_ms >= 0
    assert adm.counters[BATCH]["waited"] >= 0
    for w in waiters:
        w.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)


@pytest.mark.asyncio
async def test_interactive_overtakes_queued_batch():
    adm = make(total=1 + 1)
    a = await adm.acquire("a", BATCH, "c")
    b = await adm.acquire("b", BATCH, "c")
    batch = asyncio.create_task(adm.acquire("batch", BATCH, "c"))
    await queued(adm, 1)
    chat = asyncio.create_task(adm.acquire("chat", INTERACTIVE, "me"))
    await queued(adm, 2)
    adm.release(a)
    assert (await done(chat)).priority == INTERACTIVE
    assert "batch" not in adm.running and [e.id for e in adm.queue] == ["batch"]
    adm.release(b)
    await done(batch)


@pytest.mark.asyncio
async def test_reserved_slot_is_kept_for_interactive_and_lent_only_to_short_batch():
    adm = make(total=3, layers=[{"reservedInteractive": 1, "borrowShortTokens": 100}])
    await adm.acquire("l1", BATCH, "c", body_tokens=4000)
    await adm.acquire("l2", BATCH, "c", body_tokens=4000)
    third_long = asyncio.create_task(adm.acquire("l3", BATCH, "c", body_tokens=4000))
    unknown = asyncio.create_task(adm.acquire("u", BATCH, "c"))
    await queued(adm, 2)
    assert len(adm.running) == 2  # the reserved slot stays free
    short = await adm.acquire("s1", BATCH, "c", body_tokens=50)
    assert short.borrowed
    # Only R slots can be borrowed, and a second short request waits for an ordinary slot.
    short2 = asyncio.create_task(adm.acquire("s2", BATCH, "c", body_tokens=50))
    await queued(adm, 3)
    assert "s2" not in adm.running
    for t in (third_long, unknown, short2):
        t.cancel()
    await asyncio.gather(third_long, unknown, short2, return_exceptions=True)


@pytest.mark.asyncio
async def test_interactive_takes_the_reserved_slot_immediately():
    adm = make(total=3, layers=[{"reservedInteractive": 1}])
    await adm.acquire("l1", BATCH, "c", body_tokens=4000)
    await adm.acquire("l2", BATCH, "c", body_tokens=4000)
    chat = await asyncio.wait_for(adm.acquire("me", INTERACTIVE, "me"), 0.1)
    assert not chat.borrowed and len(adm.running) == 3


@pytest.mark.asyncio
async def test_expected_length_comes_from_request_then_history_then_size_class():
    adm = make(layers=[{"reservedInteractive": 1, "borrowShortTokens": 500}])
    assert adm.expected_length(900, "c", "XS") == (900, "request")
    assert adm.expected_length(None, "c", "XS") == (256, "size_class")
    assert adm.expected_length(None, "c", None) == (None, "unknown")
    for tokens in (100, 200, 300):
        adm.release(await adm.acquire(f"r{tokens}", BATCH, "orch/run-1"), tokens=tokens)
    assert adm.expected_length(None, "orch/run-1", "M") == (200, "history")
    assert adm.expected_length(None, "orch/run-2", "M") == (4096, "size_class")


@pytest.mark.asyncio
async def test_a_runs_own_history_lets_its_short_requests_borrow():
    adm = make(total=2, layers=[{"reservedInteractive": 1, "borrowShortTokens": 500}])
    for i in range(3):
        adm.release(await adm.acquire(f"h{i}", BATCH, "orch/run-9"), tokens=80)
    await adm.acquire("long", BATCH, "other", body_tokens=9000)
    borrowed = await asyncio.wait_for(adm.acquire("s", BATCH, "orch/run-9"), 0.1)
    assert borrowed.borrowed and borrowed.expected_source == "history"


@pytest.mark.asyncio
async def test_queue_timeout_refuses_with_429_and_retry_after():
    adm = make(total=2)
    await adm.acquire("a", BATCH, "c")
    await adm.acquire("b", BATCH, "c")
    adm.policy["batchTimeoutS"] = 0.05
    with pytest.raises(AdmissionRefused) as err:
        await adm.acquire("c", BATCH, "c")
    assert err.value.status == 429 and err.value.code == "queue_timeout"
    assert int(err.value.headers()["Retry-After"]) >= 1
    assert not adm.queue and adm.counters[BATCH]["timeouts"] == 1
    assert adm.events[-1]["action"] == "queue_timeout"


@pytest.mark.asyncio
async def test_full_queue_refuses_at_once():
    adm = make(total=2, layers=[{"maxQueue": 1}])
    await adm.acquire("a", BATCH, "c")
    await adm.acquire("b", BATCH, "c")
    waiting = asyncio.create_task(adm.acquire("c", BATCH, "c"))
    await queued(adm, 1)
    with pytest.raises(AdmissionRefused) as err:
        await adm.acquire("d", BATCH, "c")
    assert err.value.status == 429 and err.value.code == "queue_full"
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_client_leaving_while_queued_drops_its_entry_without_leaking_a_slot():
    adm = make(total=2)
    a = await adm.acquire("a", BATCH, "c")
    await adm.acquire("b", BATCH, "c")
    gone = asyncio.create_task(adm.acquire("gone", BATCH, "c"))
    await queued(adm, 1)
    gone.cancel()
    await asyncio.gather(gone, return_exceptions=True)
    assert not adm.queue
    adm.release(a)
    assert len(adm.running) == 1
    await adm.acquire("next", BATCH, "c")


@pytest.mark.asyncio
async def test_pause_holds_batch_but_not_interactive_and_resume_releases_it():
    adm = make(total=3)
    adm.set_paused(True, "me")
    batch = asyncio.create_task(adm.acquire("b", BATCH, "c"))
    await queued(adm, 1)
    chat = await asyncio.wait_for(adm.acquire("i", INTERACTIVE, "me"), 2)
    assert chat.priority == INTERACTIVE
    assert "b" not in adm.running
    adm.set_paused(False, "me")
    await done(batch)
    assert [e["action"] for e in adm.events] == ["batch_paused", "batch_resumed"]


@pytest.mark.asyncio
async def test_operator_drop_refuses_the_waiting_client_and_requires_a_reason():
    adm = make(total=2)
    await adm.acquire("a", BATCH, "c")
    await adm.acquire("b", BATCH, "c")
    waiting = asyncio.create_task(adm.acquire("w", BATCH, "orch/run-5"))
    await queued(adm, 1)
    with pytest.raises(ValueError):
        adm.drop("w", "  ", "me")
    event = adm.drop("w", "not needed", "browser-1")
    with pytest.raises(AdmissionRefused) as err:
        await waiting
    assert err.value.code == "dropped_by_operator" and err.value.status == 429
    assert "Retry-After" in err.value.headers()
    assert event["client"] == "orch/run-5" and event["reason"] == "not needed"
    assert adm.drop("w", "again", "me") is None


@pytest.mark.asyncio
async def test_operator_abort_signals_the_grant_and_records_who_and_why():
    adm = make()
    grant = await adm.acquire("r1", BATCH, "orch/run-7")
    with pytest.raises(ValueError):
        adm.abort("r1", "", "me")
    event = adm.abort("r1", "wrong model for this task", "browser-2")
    assert grant.aborted and grant.abort_reason == "wrong model for this task"
    assert event["action"] == "aborted_by_operator"
    assert event["client"] == "orch/run-7" and event["actor"] == "browser-2"
    assert adm.abort("r1", "again", "me") is None
    assert adm.abort("missing", "x", "me") is None


@pytest.mark.asyncio
async def test_native_occupancy_counts_against_capacity():
    adm = make(total=3, native=3)
    waiting = asyncio.create_task(adm.acquire("a", INTERACTIVE, "c"))
    await queued(adm, 1)
    assert "a" not in adm.running
    adm.capacity = Capacity(3, 1)
    adm.dispatch()
    await done(waiting)
    assert "a" in adm.running


@pytest.mark.asyncio
async def test_ticker_rereads_capacity_for_engines_that_free_slots_silently():
    adm = make(total=2, native=2)

    async def reader():
        return Capacity(2, 0)

    adm.reader = reader
    started = asyncio.create_task(adm.acquire("a", INTERACTIVE, "c"))
    grant = await asyncio.wait_for(started, 2)
    assert grant.waited_ms >= 0
    await adm.close()


@pytest.mark.asyncio
async def test_lifecycle_rejects_queued_requests_with_the_existing_409():
    adm = make(total=2)
    await adm.acquire("a", BATCH, "c")
    await adm.acquire("b", BATCH, "c")
    waiting = asyncio.create_task(adm.acquire("w", BATCH, "c"))
    await queued(adm, 1)
    adm.reject_queued(409, "lifecycle", "Lifecycle operation in progress; retry when ready")
    with pytest.raises(AdmissionRefused) as err:
        await waiting
    assert err.value.status == 409 and not adm.queue


def test_calendar_working_hours_and_overnight_windows():
    policy = {
        "default": 0,
        "timezone": "Europe/Athens",
        "windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "08:00",
                "end": "20:00",
                "reserved": 1,
            }
        ],
    }
    # 2026-11-23 is a Monday. Athens is UTC+2 in November.
    assert (
        reserved_at(policy, datetime.datetime(2026, 11, 23, 8, 0, tzinfo=UTC)) == 1
    )  # 10:00 local
    assert (
        reserved_at(policy, datetime.datetime(2026, 11, 23, 19, 0, tzinfo=UTC)) == 0
    )  # 21:00 local
    assert reserved_at(policy, datetime.datetime(2026, 11, 28, 10, 0, tzinfo=UTC)) == 0  # Saturday
    night = {
        "default": 0,
        "timezone": "UTC",
        "windows": [{"start": "22:00", "end": "06:00", "reserved": 2}],
    }
    assert reserved_at(night, datetime.datetime(2026, 11, 23, 3, 0, tzinfo=UTC)) == 2
    assert reserved_at(night, datetime.datetime(2026, 11, 23, 23, 0, tzinfo=UTC)) == 2
    assert reserved_at(night, datetime.datetime(2026, 11, 23, 12, 0, tzinfo=UTC)) == 0
    assert reserved_at(3, datetime.datetime.now(UTC)) == 3


@pytest.mark.asyncio
async def test_reserved_value_follows_the_calendar_and_is_capped_below_the_slot_count():
    stamp = {"now": datetime.datetime(2026, 11, 23, 8, 0, tzinfo=UTC)}
    layers = [
        {
            "reservedInteractive": {
                "default": 0,
                "timezone": "Europe/Athens",
                "windows": [{"start": "08:00", "end": "20:00", "reserved": 5}],
            }
        }
    ]
    adm = Admission(now=lambda: stamp["now"])
    adm.configure("e/m", layers, Capacity(3))
    assert adm.reserved_now() == 2  # never more than slots - 1, so batch always has a slot
    stamp["now"] = datetime.datetime(2026, 11, 23, 19, 0, tzinfo=UTC)
    assert adm.reserved_now() == 0


@pytest.mark.asyncio
async def test_override_replaces_the_calendar_until_local_midnight(tmp_path):
    adm = Admission(tmp_path / "state.json")
    adm.configure("e/m", [{"reservedInteractive": 0}], Capacity(3))
    adm.set_override(1, "me")
    assert adm.reserved_now() == 1
    assert adm.view()["reservation_override"]["value"] == 1
    adm.set_override(None, "me")
    assert adm.reserved_now() == 0
    adm.set_override(1, "me")
    adm.store.data["override"]["until"] = 1.0  # expired
    assert adm.reserved_now() == 0 and adm.store.data["override"] is None


def test_operator_state_survives_a_restart_and_a_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "admission-state.json"
    first = Admission(path)
    first.configure("e/m", [], Capacity(3))
    first.set_paused(True, "me")
    first.set_settings("e/m", {"maxQueue": 4}, "me")
    second = Admission(path)
    assert second.store.data["paused"] is True
    second.configure("e/m", [], Capacity(3))
    assert second.policy["maxQueue"] == 4
    second.set_settings("e/m", None, "me")
    second.configure("e/m", [], Capacity(3))
    assert second.policy["maxQueue"] == 32
    path.write_text("{not json")
    third = Admission(path)
    assert third.store.error and third.store.data["paused"] is False


def test_settings_validation_rejects_unknown_and_out_of_range_values():
    validate_settings({"mode": "auto", "maxQueue": 8, "sizeClassExpectedTokens": {"XS": 100}})
    bad = [
        {"mode": "fast"},
        {"maxQueue": -1},
        {"maxQueue": True},
        {"batchTimeoutS": 0},
        {"unknown": 1},
        {"sizeClassExpectedTokens": {"XL": 5}},
        {"reservedInteractive": {"windows": [{"start": "25:00", "end": "06:00", "reserved": 1}]}},
        {"reservedInteractive": {"timezone": "Not/AZone"}},
        {"reservedInteractive": -1},
    ]
    for block in bad:
        with pytest.raises(ValueError):
            validate_settings(block)


def test_settings_layers_merge_with_nested_size_classes():
    out = resolve_settings(
        {"maxQueue": 5, "sizeClassExpectedTokens": {"XS": 10}}, None, {"mode": "single"}
    )
    assert out["maxQueue"] == 5 and out["mode"] == "single"
    assert out["sizeClassExpectedTokens"] == {"XS": 10, "S": 1024, "M": 4096}


@pytest.mark.asyncio
async def test_event_log_is_cursored_and_carries_a_boot_id():
    adm = make()
    adm.set_paused(True, "me")
    adm.set_paused(False, "me")
    page = adm.events_after(None)
    assert page["boot_id"] == adm.boot_id and len(page["events"]) == 2
    later = adm.events_after(page["events"][0]["seq"])
    assert [e["action"] for e in later["events"]] == ["batch_resumed"]
    assert adm.events_after(page["next"])["events"] == []
    json.dumps(page)


@pytest.mark.asyncio
async def test_view_reports_running_queue_counters_and_p95():
    adm = make(total=2)
    a = await adm.acquire("a", BATCH, "orch/run-1", body_tokens=10)
    await adm.acquire("b", INTERACTIVE, "me")
    waiting = asyncio.create_task(adm.acquire("w", BATCH, "orch/run-2"))
    await queued(adm, 1)
    view = adm.view()
    assert view["mode"] == "queued" and view["slots_total"] == 2
    assert [r["id"] for r in view["running"]] == ["a", "b"]
    assert view["queue"][0]["id"] == "w" and view["queue"][0]["priority"] == BATCH
    assert view["counters"][BATCH]["admitted"] == 1
    adm.release(a)
    await done(waiting)
    assert adm.view()["wait_ms"][BATCH]["p95"] is not None
    json.dumps(adm.view())
