import copy

import pytest

from gateway.config import validate

BASE = {"engines": {"one": {"models": {}}}}


def config(**changes):
    cfg = copy.deepcopy(BASE)
    cfg.update(copy.deepcopy(changes))
    return cfg


def test_new_auxiliary_options_are_accepted():
    validate(
        config(
            auxiliary={
                "minimum_free_bytes": 4 * 1024**3,
                "host_baseline_bytes": 6 * 1024**3,
                "chat_growth_bytes": 0,
                "restore": "after_clean_shutdown",
                "restore_holdoff_seconds": 3600,
                "drain_seconds": {"embeddings": 30, "transcription": 600},
                "embeddings": {"models": {"mini": {"path": "/models/mini"}}},
            },
            footprints={
                "interval_seconds": 5,
                "settle_seconds": 30,
                "abort_below_available_bytes": 1024**3,
            },
            memory_footprints=[
                {
                    "kind": "chat",
                    "key": "one/m",
                    "steady_bytes": 10,
                    "peak_bytes": 12,
                    "context": 4096,
                    "note": "estimate",
                }
            ],
        )
    )
    validate(config(auxiliary={"drain_seconds": 15}))  # a single number applies to both


def test_auxiliary_model_budgets_are_optional_seeds():
    validate(config(auxiliary={"transcription": {"models": {"w": {"path": "/m/w"}}}}))
    validate(
        config(
            auxiliary={
                "transcription": {
                    "models": {"w": {"path": "/m/w", "load_bytes": 5, "request_bytes": 6}}
                }
            }
        )
    )
    for bad in ({"load_bytes": 0}, {"request_bytes": -1}, {"load_bytes": "5"}):
        with pytest.raises(ValueError):
            validate(
                config(auxiliary={"transcription": {"models": {"w": {"path": "/m/w", **bad}}}})
            )


@pytest.mark.parametrize(
    "auxiliary",
    [
        {"restore": "always"},
        {"restore_holdoff_seconds": -1},
        {"restore_holdoff_seconds": 1.5},
        {"drain_seconds": {"other": 1}},
        {"drain_seconds": {"embeddings": -1}},
        {"drain_seconds": "30"},
        {"host_baseline_bytes": 0},
        {"chat_growth_bytes": -1},
        {"unknown": 1},
    ],
)
def test_invalid_auxiliary_options_are_rejected(auxiliary):
    with pytest.raises(ValueError):
        validate(config(auxiliary=auxiliary))


@pytest.mark.parametrize(
    "footprints",
    [
        {"interval_seconds": 0},
        {"settle_seconds": -1},
        {"abort_below_available_bytes": 0},
        {"x": 1},
        "no",
    ],
)
def test_invalid_footprint_options_are_rejected(footprints):
    with pytest.raises(ValueError):
        validate(config(footprints=footprints))


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "gpu", "key": "k", "steady_bytes": 1},
        {"kind": "chat", "key": "", "steady_bytes": 1},
        {"kind": "chat", "key": "k", "steady_bytes": 0},
        {"kind": "chat", "key": "k", "steady_bytes": 1, "context": 0},
        {"kind": "chat", "key": "k", "steady_bytes": 1, "extra": 1},
        {"kind": "chat", "key": "k", "steady_bytes": 1, "note": "x" * 201},
        "nope",
    ],
)
def test_invalid_memory_footprint_seeds_are_rejected(entry):
    with pytest.raises(ValueError):
        validate(config(memory_footprints=[entry]))
    with pytest.raises(ValueError):
        validate(config(memory_footprints="nope"))
