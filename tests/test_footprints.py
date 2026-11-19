import json
import os
import stat

import pytest

from gateway.footprints import (
    Footprint,
    FootprintStore,
    aux_fingerprint,
    chat_fingerprint,
    fit,
)

GIB = 1024**3


def chat(key="e/m", steady=60 * GIB, peak=0, context=131072, fingerprint="fp", source="measured"):
    return Footprint("chat", key, steady, peak, 0, context, fingerprint, source)


def aux(kind="embeddings", key="mini", steady=GIB, request=GIB, source="measured", fp="afp"):
    return Footprint(kind, key, steady, steady + request, request, None, fp, source, 0.0, "", 1)


def test_needs_bytes_is_the_conservative_high_water_mark():
    assert chat(steady=10, peak=0).needs_bytes == 10
    assert chat(steady=10, peak=25).needs_bytes == 25
    assert aux(steady=3, request=4).needs_bytes == 7
    assert Footprint("embeddings", "x", 5, 0, 9, requests_observed=1).needs_bytes == 14
    # No request observed yet: an auxiliary model is assumed to double while serving.
    assert Footprint("embeddings", "x", 5, 0, 0).needs_bytes == 10
    assert Footprint("embeddings", "x", 5, 0, 0).request_estimate_bytes == 5


def test_seeds_from_config_are_estimates_without_fingerprint():
    from gateway.footprints import seeds_from_config

    seeds = seeds_from_config(
        {"memory_footprints": [{"kind": "chat", "key": "e/m", "steady_bytes": 7, "context": 100}]}
    )
    assert seeds[0].source == "estimate" and seeds[0].fingerprint is None
    assert seeds[0].peak_bytes == 7 and seeds[0].context == 100


def test_uncalibrated_when_no_record(tmp_path):
    store = FootprintStore(tmp_path / "f.json")
    assert store.lookup("chat", "e/m", "fp", 1000) is None


def test_measured_requires_matching_fingerprint_but_seed_does_not(tmp_path):
    seed = chat(fingerprint=None, source="estimate", steady=70 * GIB, context=262144)
    store = FootprintStore(tmp_path / "f.json", seeds=[seed])
    store.record(chat(steady=50 * GIB, fingerprint="old"))
    # Launch settings changed: the measurement is stale, the estimate still applies.
    assert store.lookup("chat", "e/m", "new", 131072).source == "estimate"
    # Same launch: the measurement wins over the estimate.
    assert store.lookup("chat", "e/m", "old", 131072).source == "measured"
    # Without a seed a stale measurement leaves the model uncalibrated.
    other = FootprintStore(tmp_path / "g.json")
    other.record(chat(fingerprint="old"))
    assert other.lookup("chat", "e/m", "new", 131072) is None


def test_record_covers_same_or_smaller_context_only(tmp_path):
    store = FootprintStore(tmp_path / "f.json")
    store.record(chat(steady=50 * GIB, context=32768))
    store.record(chat(steady=64 * GIB, context=262144))
    assert store.lookup("chat", "e/m", "fp", 16384).context == 32768  # tightest covering record
    assert store.lookup("chat", "e/m", "fp", 32768).context == 32768
    assert store.lookup("chat", "e/m", "fp", 100000).context == 262144
    assert store.lookup("chat", "e/m", "fp", 262144).context == 262144
    assert store.lookup("chat", "e/m", "fp", 262145) is None


def test_recording_keeps_highest_peaks_and_persists_privately(tmp_path):
    path = tmp_path / "state" / "f.json"
    store = FootprintStore(path)
    store.record(chat(steady=50 * GIB, peak=55 * GIB))
    merged = store.record(chat(steady=51 * GIB, peak=52 * GIB))
    assert merged.steady_bytes == 51 * GIB and merged.peak_bytes == 55 * GIB
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    reloaded = FootprintStore(path)
    assert reloaded.lookup("chat", "e/m", "fp", 1).peak_bytes == 55 * GIB
    assert reloaded.error is None


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"version": 99, "records": []}', '{"version": 1, "records": [{"x": 1}]}'],
)
def test_damaged_file_fails_closed(tmp_path, content):
    path = tmp_path / "f.json"
    path.write_text(content)
    store = FootprintStore(path)
    assert store.records == {} and store.error
    assert store.lookup("chat", "e/m", "fp", 1) is None


def test_invalid_records_are_rejected(tmp_path):
    store = FootprintStore(tmp_path / "f.json")
    for bad in (
        Footprint("chat", "k", 0),
        Footprint("other", "k", 1),
        Footprint("chat", "", 1),
        Footprint("chat", "k", 1, source="guess"),
        Footprint("chat", "k", 1, context=0),
        Footprint("chat", "k", True),
    ):
        with pytest.raises(ValueError):
            store.record(bad)
    assert FootprintStore(tmp_path / "x.json", seeds=[Footprint("chat", "k", 0)]).seeds == []


def test_forget_removes_all_records_for_a_model(tmp_path):
    path = tmp_path / "f.json"
    store = FootprintStore(path)
    store.record(chat(context=1000))
    store.record(chat(context=2000))
    store.record(chat(key="other"))
    assert store.forget("chat", "e/m") == 2
    assert FootprintStore(path).lookup("chat", "e/m", "fp", 1) is None
    assert FootprintStore(path).lookup("chat", "other", "fp", 1) is not None


def test_listing_marks_stale_measurements(tmp_path):
    store = FootprintStore(tmp_path / "f.json")
    store.record(chat(fingerprint="old"))
    rows = store.listing({("chat", "e/m"): "new"})
    assert rows[0]["stale"] is True
    assert store.listing({("chat", "e/m"): "old"})[0]["stale"] is False


def test_fit_ok_over_budget_and_uncalibrated():
    total, reserve, base = 124 * GIB, 4 * GIB, 6 * GIB  # budget 114
    ok = fit(
        total,
        reserve,
        base,
        [("chat", "a", chat(steady=90 * GIB)), ("embeddings", "m", aux(steady=GIB, request=GIB))],
    )
    assert ok.fits and ok.reason == "ok" and ok.needed_bytes == 92 * GIB
    exact = fit(
        total,
        reserve,
        base,
        [("chat", "a", chat(steady=112 * GIB)), ("embeddings", "m", aux(steady=GIB, request=GIB))],
    )
    assert exact.fits and exact.needed_bytes == exact.budget_bytes  # equality fits
    over = fit(
        total,
        reserve,
        base,
        [("chat", "a", chat(steady=113 * GIB)), ("embeddings", "m", aux(steady=GIB, request=GIB))],
    )
    assert not over.fits and over.reason == "over_budget" and over.short_bytes == GIB
    missing = fit(total, reserve, base, [("chat", "a", None), ("embeddings", "m", aux())])
    assert not missing.fits and missing.reason == "uncalibrated" and missing.missing == ("chat:a",)


def test_fit_is_model_agnostic_larger_auxiliary_changes_what_fits():
    """No model family is special-cased: only the recorded numbers decide."""
    total, reserve, base = 124 * GIB, 4 * GIB, 6 * GIB
    big_chat = chat(steady=105 * GIB)
    small_asr = aux("transcription", "small-asr", steady=2 * GIB, request=2 * GIB)
    large_asr = aux("transcription", "large-asr", steady=10 * GIB, request=6 * GIB)
    emb = aux("embeddings", "mini", steady=GIB, request=GIB)
    with_small = fit(
        total,
        reserve,
        base,
        [("chat", "a", big_chat), ("transcription", "s", small_asr), ("embeddings", "m", emb)],
    )
    with_large = fit(
        total,
        reserve,
        base,
        [("chat", "a", big_chat), ("transcription", "l", large_asr), ("embeddings", "m", emb)],
    )
    assert with_small.fits is True
    assert with_large.fits is False and with_large.reason == "over_budget"
    # ...and the same large model fits next to a smaller chat model.
    assert fit(
        total,
        reserve,
        base,
        [
            ("chat", "b", chat(steady=60 * GIB)),
            ("transcription", "l", large_asr),
            ("embeddings", "m", emb),
        ],
    ).fits


def test_chat_fingerprint_ignores_context_placeholder_but_not_launch_settings():
    spec = {
        "_argv_template": ["run", "--ctx", "{context}", "-e", "SLOTS=1"],
        "argv": ["run", "--ctx", "4096"],
        "env": {"A": "1"},
    }
    same_other_ctx = {**spec, "argv": ["run", "--ctx", "8192"]}
    assert chat_fingerprint("e", "m", spec) == chat_fingerprint("e", "m", same_other_ctx)
    changed = {**spec, "_argv_template": ["run", "--ctx", "{context}", "-e", "SLOTS=3"]}
    assert chat_fingerprint("e", "m", spec) != chat_fingerprint("e", "m", changed)
    assert chat_fingerprint("e", "m", spec) != chat_fingerprint("e", "other", spec)
    assert chat_fingerprint("e", "m", spec) != chat_fingerprint(
        "e", "m", {**spec, "env": {"A": "2"}}
    )


def test_aux_fingerprint_ignores_caches_and_mtimes_but_not_weights(tmp_path):
    import time

    model = tmp_path / "model"
    model.mkdir()
    (model / "weights.bin").write_bytes(b"x" * 100)
    spec = {"path": str(model), "threads": 2}
    base = aux_fingerprint("embeddings", "mini", spec)
    # Lock/cache files, hidden directories and plain mtime changes leave it alone.
    (model / ".lock").write_text("")
    (model / ".cache").mkdir()
    (model / ".cache" / "blob").write_bytes(b"y" * 500)
    (model / "__pycache__").mkdir()
    os.utime(model / "weights.bin", (time.time() + 99, time.time() + 99))
    assert aux_fingerprint("embeddings", "mini", spec) == base
    # A real change to the weights (size) or a new weight file changes it.
    (model / "weights.bin").write_bytes(b"x" * 200)
    changed = aux_fingerprint("embeddings", "mini", spec)
    assert changed != base
    (model / "extra.bin").write_bytes(b"z")
    assert aux_fingerprint("embeddings", "mini", spec) != changed
    # A single-file model path works too; a missing path does not crash.
    assert aux_fingerprint("embeddings", "mini", {"path": str(model / "weights.bin")})
    assert aux_fingerprint("embeddings", "mini", {"path": str(tmp_path / "missing")})


def test_aux_fingerprint_tracks_path_and_threads(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    base = aux_fingerprint("embeddings", "mini", {"path": str(model), "threads": 2})
    assert base == aux_fingerprint("embeddings", "mini", {"path": str(model), "threads": 2})
    assert base != aux_fingerprint("embeddings", "mini", {"path": str(model), "threads": 4})
    assert base != aux_fingerprint("transcription", "mini", {"path": str(model), "threads": 2})
    assert json.dumps(base)  # serializable
