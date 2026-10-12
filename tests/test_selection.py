import importlib.util

import pytest


@pytest.mark.asyncio
async def test_engine_selection_requires_no_model_process(tmp_path):
    assert importlib.util.find_spec("gateway") is not None, "Gateway package not implemented"
    from gateway.core import Manager

    m = Manager({"engines": {"strata": {"label": "Strata", "models": {}}}}, tmp_path)
    await m.select_engine("strata")
    assert m.engine == "strata"
    assert m.model is None
    assert m.phase == "selected"
    assert m.process is None
