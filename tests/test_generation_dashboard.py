"""Dashboard wording for generation metrics, run through Node against the shipped app.js."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def describe(payload):
    script = (ROOT / "gateway/static/app.js").read_text()
    source = re.search(r"function describeGeneration\(g\) \{.*?\n\}\n", script, re.S).group(0)
    code = f"{source}\nconsole.log(describeGeneration({json.dumps(payload)}));"
    result = subprocess.run([NODE, "-e", code], capture_output=True, text=True, check=True)
    return result.stdout.strip()


LAST = {
    "outcome": "complete",
    "source": "engine",
    "generation": {"tokens": 1219, "tokens_per_second": 25.64},
    "prefill": {"tokens": 2048, "tokens_per_second": 173.0, "cached_tokens": 0},
    "time_to_first_token_seconds": 11.9,
    "draft": {"proposed": 1578, "accepted": 990, "acceptance": 0.627},
}


def test_page_and_poll_wire_the_generation_line():
    page = (ROOT / "gateway/static/index.html").read_text()
    script = (ROOT / "gateway/static/app.js").read_text()
    assert 'id="generation"' in page
    assert "describeGeneration(s.generation)" in script


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_wording_for_every_state():
    assert describe(None) == "No generation data since the gateway started"
    assert describe({"live": None, "last": None}).startswith("No generation data")
    assert (
        describe({"live": {"state": "prefill", "elapsed_seconds": 3.14, "tokens": 0}})
        == "Prefill · waiting for first token · 3.1 s"
    )
    live = {"state": "generating", "tokens": 120, "tokens_per_second": 23.44}
    assert describe({"live": live}) == "Generating · 23.4 tok/s · 120 tokens (estimate)"
    live["tokens_per_second"] = None
    assert describe({"live": live}) == "Generating · measuring · 120 tokens (estimate)"
    assert describe({"live": None, "last": LAST}) == (
        "Last: Generation 25.6 tok/s · Prefill 173 tok/s · first token 11.9 s · "
        "1,219 tokens · 63% of drafts accepted"
    )


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_unknown_values_are_labelled_not_invented():
    last = {
        **LAST,
        "source": "estimated",
        "outcome": "cancelled",
        "prefill": {"tokens": None, "tokens_per_second": None, "cached_tokens": None},
        "generation": {"tokens": 4, "tokens_per_second": None},
        "time_to_first_token_seconds": None,
        "draft": None,
    }
    assert describe({"live": None, "last": last}) == (
        "Last: Generation unknown · Prefill unknown · 4 tokens · estimated · cancelled"
    )


def test_documentation_names_the_api_and_the_limits():
    docs = (ROOT / "docs/generation-metrics.md").read_text()
    for needle in ("/api/status", "predicted_per_second", "prompt_per_second", "never derived"):
        assert needle in docs
    assert "generation-metrics.md" in (ROOT / "README.md").read_text()
