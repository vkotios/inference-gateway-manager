def test_drm_connectors_are_not_reported_as_gpus(tmp_path, monkeypatch):
    from gateway import telemetry as module

    for name in ["card1", "card1-DP-1"]:
        (tmp_path / name / "device").mkdir(parents=True)
    (tmp_path / "card1" / "device" / "gpu_busy_percent").write_text("42")
    monkeypatch.setattr(module, "Path", lambda value: tmp_path)
    result = module.telemetry()
    assert [gpu["name"] for gpu in result["gpus"]] == ["card1"]
    assert result["gpus"][0]["utilization_percent"] == 42
