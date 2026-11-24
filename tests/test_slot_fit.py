import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_settings import admin, record_slots, slots_app

from gateway.client import GatewayClient, GatewayError
from gateway.config import validate
from gateway.footprints import CHAT, Footprint
from gateway.mcp import create_server
from gateway.slot_fit import GIB, fit_slots, slot_choice, slot_profile

CASES = json.loads((Path(__file__).parent / "fixtures" / "slot_fit_cases.json").read_text())
INSUFFICIENT = "not enough measurements: calibrate slots"


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_slot_fit_margin_must_be_a_positive_integer(value):
    with pytest.raises(ValueError, match=r"footprints\.slot_fit_margin_bytes"):
        validate({"engines": {"one": {}}, "footprints": {"slot_fit_margin_bytes": value}})


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_fit_cases(case):
    records = case["by_slots"]
    fit = fit_slots(records, case["fit"]["budget"], case["floor"])
    assert fit == case["fit"]
    for context, expected in zip(case["contexts"], case["selectable"], strict=True):
        assert [
            int(count)
            for count in records
            if slot_choice(records, fit, case["default"], int(count), context)["reason"] is None
        ] == expected


@pytest.mark.parametrize(
    "pairs", [[(100, 10)], [(100, 10), (149, 11)], [(100, 12), (150, 10)], [(100, 10), (200, 30)]]
)
def test_fit_rejects_insufficient_spread_negative_slope_or_negative_intercept(pairs):
    records = {"1": [{"context": p, "needs_bytes": need} for p, need in pairs]}
    assert fit_slots(records, 100, 2)["reason"] == "not enough measurements"


def test_margin_covers_worst_positive_residual_and_the_floor():
    records = {
        "1": [
            {"context": 100, "needs_bytes": 20},
            {"context": 200, "needs_bytes": 42},
            {"context": 300, "needs_bytes": 40},
        ]
    }
    assert fit_slots(records, 100, 2)["margin"] == 8
    assert fit_slots(records, 100, 9)["margin"] == 9


def test_covering_measurement_overrides_the_estimate_in_both_directions():
    fit = {"a": 20 * GIB, "k": GIB / 1024, "margin": 2 * GIB, "points": 2, "budget": 30 * GIB}
    records = {
        "2": [{"context": 1024, "needs_bytes": 31 * GIB}],
        "3": [{"context": 4096, "needs_bytes": 26 * GIB}],
    }
    assert slot_choice(records, fit, 1, 2, 1024)["reason"] == "needs ~31.0 GiB, budget 30.0 GiB"
    assert slot_choice(records, fit, 1, 3, 4096) == {
        "needs_bytes": 26 * GIB,
        "source": "measured",
        "reason": None,
    }
    assert slot_choice(records, fit, 1, 3, 4097)["reason"] == "needs ~34.0 GiB, budget 30.0 GiB"
    assert slot_choice(records, fit, 1, 1, 32768)["reason"] is None


async def test_profile_uses_only_matching_measured_peaks(tmp_path):
    manager = slots_app(tmp_path, measured=False).state.manager
    try:
        store = manager.tracker.store
        store.seeds.append(
            Footprint(CHAT, "one/fixture", 4 * GIB, context=65536, source="estimate")
        )
        store.record(Footprint(CHAT, "one/fixture", 8 * GIB, context=65536, fingerprint="stale"))
        record_slots(manager, 1, 262144, 32 * GIB)
        records, fit = slot_profile(manager, "one", "fixture")
        assert records["1"] == [{"context": 262144, "needs_bytes": 32 * GIB}]
        assert fit["points"] == 1 and fit["reason"] == "not enough measurements"
    finally:
        await manager.close()


async def test_unselectable_count_is_refused_by_api_client_and_mcp(tmp_path):
    app = slots_app(tmp_path, measured=False)
    manager = app.state.manager
    target = {"engine": "one", "model": "fixture"}
    try:
        async with admin(app) as client:
            loaded = await client.post("/api/model", json=target)
            assert loaded.status_code == 200, loaded.text
            process = manager.process
            refused = await client.post("/api/model", json={**target, "slots": 2, "force": True})
            assert (refused.status_code, refused.json()["detail"]) == (409, INSUFFICIENT)
        async with GatewayClient(
            "http://testserver", "a" * 32, transport=httpx.ASGITransport(app=app)
        ) as remote:
            with pytest.raises(GatewayError, match=f"409: {INSUFFICIENT}"):
                await remote.load(**target, slots=2)
            from mcp.server.fastmcp.exceptions import ToolError

            with pytest.raises(ToolError, match=f"409: {INSUFFICIENT}"):
                await create_server(remote).call_tool("gateway_load", {**target, "slots": 2})
        assert manager.process is process and manager.selected_settings["slots"] == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("abort", [False, True])
async def test_calibrate_slots_records_two_points_or_aborts(tmp_path, abort, monkeypatch):
    app = slots_app(tmp_path, measured=False)
    manager = app.state.manager
    tracker = manager.tracker
    tracker.settle_seconds, tracker.interval = 0, 0.01
    tracker.sample_used = lambda: 0
    tracker.sample_tree = lambda pids: (GIB + manager.selected_settings["context"] * 4096, 0)
    manager.sample_available = lambda: (
        0 if abort and manager.selected_settings["context"] == 65536 else 16 * GIB
    )
    starts = []
    start_backend = manager._start_backend

    async def start(spec, settings, key, fingerprint):
        starts.append((settings["slots"], settings["context"]))
        await start_backend(spec, settings, key, fingerprint)

    monkeypatch.setattr(manager, "_start_backend", start)
    try:
        async with admin(app) as client:
            response = await client.post(
                "/api/footprints/calibrate",
                json={"engine": "one", "model": "fixture", "slots": True},
            )
            assert starts == [(1, 262144), (1, 65536)]
            if abort:
                assert response.status_code == 503
                assert "available memory fell to 0 MiB" in response.text
                assert manager.process is None
                assert [r.context for r in tracker.store.records.values()] == [262144]
            else:
                assert response.status_code == 200, response.text
                body = response.json()
                assert [r["context"] for r in body["records"]] == [262144, 65536]
                assert {r.context for r in tracker.store.records.values()} == {262144, 65536}
                assert body["fit"]["points"] == 2 and body["fit"]["k"] == 4096
                assert manager.selected_settings["context"] == body["context"] == 65536
    finally:
        await manager.close()


@pytest.mark.parametrize("stage", ["startup", "settling", "measured"])
async def test_first_load_only_watches_unmeasured_combinations(tmp_path, stage):
    app = slots_app(tmp_path, measured=False)
    manager = app.state.manager
    context = 65536
    if stage == "startup":
        busy = tmp_path / "warmup-busy"
        busy.touch()
        manager.config["engines"]["one"]["models"]["fixture"]["argv"].append(
            f"--busy-while-exists={busy}"
        )
    # A larger measurement covers this context for fit, but only the exact point skips the guard.
    record_slots(manager, 1, context if stage == "measured" else 262144, GIB)
    calls = []
    low = stage != "settling"

    def available():
        calls.append(True)
        return 512 * 1024**2 if low else 16 * GIB

    manager.sample_available = available
    reason = "aborted: available memory fell to 512 MiB"
    try:
        async with admin(app) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/api/model", json={"engine": "one", "model": "fixture", "context": context}
                ),
                8,
            )
            if stage == "startup":
                assert response.status_code == 503 and response.json()["detail"] == reason
                assert calls and manager.process is None
            else:
                assert response.status_code == 200 and response.json()["phase"] == "ready"
                if stage == "settling":
                    watcher = manager.memory_watch_task
                    assert watcher is not None
                    low = True
                    await asyncio.wait_for(watcher, 8)
                    assert manager.process is None
                    assert (await client.get("/api/status")).json()["error"] == reason
                else:
                    assert not calls and manager.memory_watch_task is None
    finally:
        await manager.close()
