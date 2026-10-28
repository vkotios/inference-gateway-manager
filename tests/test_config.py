import importlib.util

import pytest


@pytest.mark.parametrize(
    "origins",
    [
        "https://example.lan",
        None,
        [None],
        ["*"],
        ["https://*.example.lan"],
        ["ftp://example.lan"],
        ["https://example.lan/"],
        ["https://example.lan/path"],
        ["https://example.lan?"],
        ["https://example.lan#"],
        ["https://user:pass@example.lan"],
        ["https://example.lan:bad"],
        ["https://example.lan:65536"],
        ["https://example.lan:0"],
        ["https://example.lan:"],
        ["https://"],
        ["https://bad host"],
        [" https://example.lan"],
        ["https://example.lan\n"],
        ["https://example.lan\\evil"],
    ],
)
def test_invalid_origins_are_rejected_at_config_load(origins):
    from gateway.config import validate

    with pytest.raises(ValueError, match="allowed_origins"):
        validate({"engines": {"one": {"models": {}}}, "allowed_origins": origins})


@pytest.mark.parametrize(
    "origins",
    [
        [],
        ["https://example.lan"],
        ["http://127.0.0.1:9000"],
        ["https://[::1]:9443", "http://localhost"],
    ],
)
def test_exact_http_origins_are_preserved(origins):
    from gateway.config import validate

    config = {"engines": {"one": {"models": {}}}, "allowed_origins": origins}
    assert validate(config)["allowed_origins"] == origins


def test_config_rejects_public_backend_and_empty_process_matcher():
    assert importlib.util.find_spec("gateway.config") is not None, (
        "Configuration validation missing"
    )
    from gateway.config import validate

    good = {
        "host": "127.0.0.1",
        "port": 9000,
        "native_matchers": [{"exe": "fixture"}],
        "engines": {
            "fixture": {
                "models": {
                    "fixture": {
                        "argv": ["fixture"],
                        "base_url": "http://127.0.0.1:9001",
                        "context": 4096,
                    }
                }
            }
        },
    }
    assert validate(good) == good
    good["engines"]["fixture"]["models"]["fixture"]["base_url"] = "https://external.example"
    with pytest.raises(ValueError, match="loopback"):
        validate(good)
    good["engines"]["fixture"]["models"]["fixture"]["base_url"] = "http://127.0.0.1:9001"
    good["native_matchers"] = [{}]
    with pytest.raises(ValueError, match="matcher"):
        validate(good)


@pytest.mark.parametrize("value", [True, "guessed", {}, [], 1])
def test_native_generation_metrics_provider_is_explicit(value):
    from gateway.config import validate_runtime

    spec = {"argv": ["fixture"], "base_url": "http://127.0.0.1:8011", "generation_metrics": value}
    with pytest.raises(ValueError, match="generation_metrics"):
        validate_runtime(spec)


def test_lucebox_generation_metrics_provider_is_accepted():
    from gateway.config import validate_runtime

    validate_runtime(
        {"argv": ["fixture"], "base_url": "http://127.0.0.1:8011", "generation_metrics": "lucebox"}
    )
