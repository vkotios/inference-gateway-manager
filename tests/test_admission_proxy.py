"""Contract tests: the chat admission broker behind POST /v1/chat/completions."""

import asyncio
import json

import httpx
import pytest

from gateway.app import create_app

ADMIN, INFER = "a" * 32, "b" * 32


class Alive:
    def poll(self):
        return None


class Stream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        async for part in self.parts:
            yield part

    async def aclose(self):
        pass


class Backend(httpx.AsyncBaseTransport):
    """A stub engine whose responses stay open until the test releases them."""

    def __init__(self):
        self.bodies = []
        self.gates = []
        self.closed = 0

    async def handle_async_request(self, request):
        self.bodies.append(json.loads(request.content))
        gate = asyncio.Event()
        self.gates.append(gate)

        async def parts():
            yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            await gate.wait()
            yield (
                b'data: {"choices":[{"delta":{"content":"b"}}],'
                b'"usage":{"completion_tokens":60}}\n\ndata: [DONE]\n\n'
            )

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream(parts())
        )

    def release(self, index=None):
        for i, gate in enumerate(self.gates):
            if index is None or i == index:
                gate.set()


class Harness:
    def __init__(self, tmp_path, slots=3, admission=None, top=None):
        spec = {
            "base_url": "http://127.0.0.1:1",
            "probe": "gateway_only",
            "chat_slots": slots,
            "upstream_model": "up",
        }
        if admission is not None:
            spec["admission"] = admission
        cfg = {
            "allowed_hosts": ["testserver"],
            "engines": {"one": {"models": {"fixture": spec}}},
        }
        if top is not None:
            cfg["admission"] = top
        self.app = create_app(cfg, ADMIN, INFER, tmp_path)
        self.manager = self.app.state.manager
        self.admission = self.app.state.admission
        self.backend = Backend()
        self.manager.http = httpx.AsyncClient(transport=self.backend)
        self.manager.engine, self.manager.model = "one", "fixture"
        self.manager.phase, self.manager.process = "ready", Alive()
        transport = httpx.ASGITransport(app=self.app)
        self.chat = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + INFER},
        )
        self.admin = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + ADMIN},
        )
        self.tasks = []

    def post(self, priority=None, client="test", **extra):
        headers = {"X-Client-Name": client}
        if priority:
            headers["X-IGM-Priority"] = priority
        headers.update(extra.pop("headers", {}))
        payload = {"model": "fixture", "stream": True, "messages": []}
        payload.update(extra)
        task = asyncio.create_task(
            self.chat.post("/v1/chat/completions", json=payload, headers=headers)
        )
        self.tasks.append(task)
        return task

    async def until(self, check, timeout=2.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if check():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not reached")

    async def close(self):
        self.backend.release()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.chat.aclose()
        await self.admin.aclose()
        self.manager.process = None
        self.manager.model = None
        await self.admission.close()
        await self.manager.close()


@pytest.fixture
async def harness(tmp_path):
    made = []

    def build(**kw):
        h = Harness(tmp_path, **kw)
        made.append(h)
        return h

    yield build
    for h in made:
        await h.close()


@pytest.mark.asyncio
async def test_more_requests_than_slots_queue_instead_of_409(harness):
    h = harness(slots=2)
    first, second, third = h.post("batch"), h.post("batch"), h.post("batch")
    await h.until(lambda: len(h.backend.bodies) == 2)
    assert len(h.admission.queue) == 1 and not third.done()
    status = (await h.admin.get("/api/status")).json()
    assert status["chat_slots"] == {
        "total": 2,
        "available": 0,
        "reserved_interactive": 0,
        "queued": 1,
    }
    assert status["admission"]["queue"][0]["priority"] == "batch"
    h.backend.release(0)
    await h.until(lambda: len(h.backend.bodies) == 3)
    h.backend.release()
    results = await asyncio.gather(first, second, third)
    assert [r.status_code for r in results] == [200, 200, 200]
    assert results[2].headers["x-igm-queue-wait-ms"]
    assert "x-igm-queue-wait-ms" not in results[0].headers
    assert not h.manager.active and not h.admission.running


@pytest.mark.asyncio
async def test_single_slot_load_keeps_the_409_refusal(harness):
    h = harness(slots=1)
    first = h.post()
    await h.until(lambda: len(h.backend.bodies) == 1)
    refused = await h.chat.post("/v1/chat/completions", json={"model": "fixture", "messages": []})
    assert refused.status_code == 409
    assert "chat slot is free" in refused.json()["detail"]
    h.backend.release()
    assert (await first).status_code == 200


@pytest.mark.asyncio
async def test_missing_priority_is_interactive_and_a_bad_value_is_400(harness):
    h = harness()
    task = h.post()
    await h.until(lambda: len(h.backend.bodies) == 1)
    running = (await h.admin.get("/api/status")).json()["admission"]["running"]
    assert running[0]["priority"] == "interactive"
    h.backend.release()
    assert (await task).headers["x-igm-priority"] == "interactive"
    bad = await h.chat.post(
        "/v1/chat/completions",
        json={"model": "fixture", "messages": []},
        headers={"X-IGM-Priority": "urgent"},
    )
    assert bad.status_code == 400 and bad.headers["x-igm-code"] == "bad_priority"


@pytest.mark.asyncio
async def test_interactive_request_overtakes_a_queued_batch_request(harness):
    h = harness(slots=2)
    h.post("batch", client="b1")
    h.post("batch", client="b2")
    await h.until(lambda: len(h.backend.bodies) == 2)
    h.post("batch", client="late-batch")
    await h.until(lambda: len(h.admission.queue) == 1)
    h.post("interactive", client="me")
    await h.until(lambda: len(h.admission.queue) == 2)
    h.backend.release(0)
    await h.until(lambda: len(h.backend.bodies) == 3)
    assert (
        h.admission.running[
            next(i for i, g in h.admission.running.items() if g.client == "me")
        ].priority
        == "interactive"
    )
    assert [e.client for e in h.admission.queue] == ["late-batch"]


@pytest.mark.asyncio
async def test_reserved_slot_goes_to_interactive_unless_the_batch_request_is_short(harness):
    h = harness(slots=3, admission={"reservedInteractive": 1, "borrowShortTokens": 100})
    h.post("batch", max_tokens=4000)
    h.post("batch", max_tokens=4000)
    await h.until(lambda: len(h.backend.bodies) == 2)
    h.post("batch", max_tokens=4000)
    await h.until(lambda: len(h.admission.queue) == 1)
    assert len(h.admission.running) == 2  # the third slot is held back for interactive
    h.post("batch", max_tokens=20)  # short: may borrow it
    await h.until(lambda: len(h.backend.bodies) == 3)
    borrowed = [g for g in h.admission.running.values() if g.borrowed]
    assert len(borrowed) == 1 and borrowed[0].expected_source == "request"


@pytest.mark.asyncio
async def test_a_full_queue_and_a_timeout_return_429_with_retry_after(harness):
    h = harness(slots=2, admission={"maxQueue": 1, "batchTimeoutS": 0.1})
    h.post("batch")
    h.post("batch")
    await h.until(lambda: len(h.backend.bodies) == 2)
    waiting = h.post("batch")
    await h.until(lambda: len(h.admission.queue) == 1)
    full = await h.chat.post(
        "/v1/chat/completions",
        json={"model": "fixture", "messages": []},
        headers={"X-IGM-Priority": "batch"},
    )
    assert full.status_code == 429 and full.headers["x-igm-code"] == "queue_full"
    assert int(full.headers["retry-after"]) >= 1
    timed_out = await waiting
    assert timed_out.status_code == 429 and timed_out.headers["x-igm-code"] == "queue_timeout"
    actions = [e["action"] for e in h.admission.events]
    assert "queue_full" in actions and "queue_timeout" in actions


@pytest.mark.asyncio
async def test_operator_abort_ends_a_stream_with_a_specific_error_and_frees_the_slot(harness):
    h = harness(slots=2)
    task = h.post("batch", client="orch/run-42")
    await h.until(lambda: len(h.backend.bodies) == 1)
    request_id = next(iter(h.admission.running))
    none = await h.admin.post(f"/api/admission/requests/{request_id}/abort", json={"reason": ""})
    assert none.status_code == 422  # a reason is required
    done = await h.admin.post(
        f"/api/admission/requests/{request_id}/abort", json={"reason": "wrong model"}
    )
    assert done.status_code == 200
    response = await task
    assert response.status_code == 200
    error = json.loads(response.text.strip().split("data: ")[-1])["error"]
    assert error["code"] == "aborted_by_operator" and "wrong model" in error["message"]
    await h.until(lambda: not h.admission.running and not h.manager.active)
    events = (await h.admin.get("/api/admission/events")).json()
    event = events["events"][-1]
    assert event["action"] == "aborted_by_operator"
    assert event["client"] == "orch/run-42" and event["reason"] == "wrong model"
    assert event["actor"] == "control-key" and event["request_id"] == request_id
    assert (
        await h.admin.post(f"/api/admission/requests/{request_id}/abort", json={"reason": "x"})
    ).status_code == 404


@pytest.mark.asyncio
async def test_abort_before_the_engine_answers_gives_499_with_the_code(harness):
    h = harness()
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()

    h.backend.handle_async_request = slow
    task = h.post("batch")
    await h.until(lambda: len(h.admission.running) == 1)
    await h.until(lambda: any("grant" in v for v in h.manager.active.values()))
    request_id = next(iter(h.admission.running))
    await asyncio.sleep(0.05)
    await h.admin.post(f"/api/admission/requests/{request_id}/abort", json={"reason": "stuck"})
    response = await task
    assert response.status_code == 499 and response.headers["x-igm-code"] == "aborted_by_operator"
    await h.until(lambda: not h.admission.running)


@pytest.mark.asyncio
async def test_operator_drop_gives_the_waiting_client_429(harness):
    h = harness(slots=2)
    h.post("batch")
    h.post("batch")
    await h.until(lambda: len(h.backend.bodies) == 2)
    waiting = h.post("batch", client="orch/run-9")
    await h.until(lambda: len(h.admission.queue) == 1)
    request_id = h.admission.queue[0].id
    ok = await h.admin.post(f"/api/admission/queue/{request_id}/drop", json={"reason": "stale"})
    assert ok.status_code == 200
    response = await waiting
    assert response.status_code == 429
    assert response.headers["x-igm-code"] == "dropped_by_operator"
    assert "Retry-After" in response.headers


@pytest.mark.asyncio
async def test_pausing_batch_holds_new_batch_requests_but_not_interactive(harness):
    h = harness(slots=3)
    paused = await h.admin.post("/api/admission/pause", json={"paused": True})
    assert paused.json()["paused_batch"] is True
    batch = h.post("batch")
    await h.until(lambda: len(h.admission.queue) == 1)
    chat = h.post("interactive")
    await h.until(lambda: len(h.backend.bodies) == 1)
    assert not batch.done()
    await h.admin.post("/api/admission/pause", json={"paused": False})
    await h.until(lambda: len(h.backend.bodies) == 2)
    h.backend.release()
    assert (await batch).status_code == 200 and (await chat).status_code == 200


@pytest.mark.asyncio
async def test_unloading_the_model_refuses_queued_requests_with_409(harness):
    h = harness(slots=2)
    h.post("batch")
    h.post("batch")
    await h.until(lambda: len(h.backend.bodies) == 2)
    waiting = h.post("batch")
    await h.until(lambda: len(h.admission.queue) == 1)
    h.manager.drain()
    response = await waiting
    assert response.status_code == 409 and response.headers["x-igm-code"] == "lifecycle"


@pytest.mark.asyncio
async def test_a_queued_client_that_disconnects_leaves_no_entry(harness):
    h = harness(slots=2)
    h.post("batch")
    h.post("batch")
    await h.until(lambda: len(h.backend.bodies) == 2)
    gone = h.post("batch")
    await h.until(lambda: len(h.admission.queue) == 1)
    gone.cancel()
    await asyncio.gather(gone, return_exceptions=True)
    await h.until(lambda: not h.admission.queue)
    assert len(h.admission.running) == 2


@pytest.mark.asyncio
async def test_admission_api_needs_the_control_key_and_validates_settings(harness):
    h = harness()
    assert (await h.chat.get("/api/admission")).status_code == 403
    assert (await h.chat.get("/api/admission/events")).status_code == 403
    state = (await h.admin.get("/api/admission")).json()
    assert state["mode"] == "queued" and state["slots_total"] == 3
    bad = await h.admin.post("/api/admission/settings", json={"settings": {"maxQueue": -5}})
    assert bad.status_code == 400
    ok = await h.admin.post(
        "/api/admission/settings", json={"settings": {"maxQueue": 7, "mode": "single"}}
    )
    assert ok.json()["settings"]["maxQueue"] == 7 and ok.json()["mode"] == "single"
    assert ok.json()["config_settings"]["maxQueue"] == 32
    reset = await h.admin.post("/api/admission/settings", json={"settings": None})
    assert reset.json()["settings"]["maxQueue"] == 32
    held = await h.admin.post("/api/admission/reservation", json={"value": 1})
    assert held.json()["reservation_override"]["value"] == 1
    cleared = await h.admin.post("/api/admission/reservation", json={"value": None})
    assert cleared.json()["reservation_override"] is None


@pytest.mark.asyncio
async def test_event_cursor_returns_only_newer_events(harness):
    h = harness()
    await h.admin.post("/api/admission/pause", json={"paused": True})
    page = (await h.admin.get("/api/admission/events")).json()
    cursor = page["next"]
    await h.admin.post("/api/admission/pause", json={"paused": False})
    later = (await h.admin.get(f"/api/admission/events?after={cursor}")).json()
    assert [e["action"] for e in later["events"]] == ["batch_resumed"]
    assert later["boot_id"] == page["boot_id"]


@pytest.mark.asyncio
async def test_finished_requests_teach_the_run_history_used_for_borrowing(harness):
    h = harness(slots=3, admission={"reservedInteractive": 1, "borrowShortTokens": 100})
    for _ in range(3):
        task = h.post("batch", client="orch/run-5")
        await h.until(lambda: h.backend.bodies)
        h.backend.release()
        await task
        h.backend.bodies.clear()
        h.backend.gates.clear()
    assert list(h.admission.history["orch/run-5"]) == [60, 60, 60]
    assert h.admission.expected_length(None, "orch/run-5", None) == (60, "history")
