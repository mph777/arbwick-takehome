"""The live call is checked against the installed SDK, not against memory.

`_call_live` is the one code path the offline test suite cannot execute: it needs
a key and a network. That made it the one place where a wrong argument survived
until a live run - `temperature=` was removed from `Messages.create` in anthropic
1.0.0, and the mistake surfaced 52 dates into a weekly run, on the first date
that actually reached Stage 3.

These tests introspect the installed SDK instead. They do not call the API, cost
nothing, and would have caught it before the run started.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import config as cfg
from pipeline import allocation

anthropic = pytest.importorskip("anthropic")


def create_kwargs_used() -> set[str]:
    """Keyword names passed to client.messages.create() in allocation.py.

    Read out of the source rather than by calling it, because calling it is
    exactly what these tests are avoiding.
    """
    src = textwrap.dedent(inspect.getsource(allocation._call_live))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create"):
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError("no messages.create() call found in _call_live")


def test_every_argument_we_pass_exists_in_the_installed_sdk():
    from anthropic.resources.messages import Messages

    accepted = set(inspect.signature(Messages.create).parameters)
    unknown = create_kwargs_used() - accepted
    assert not unknown, (
        f"allocation._call_live passes {sorted(unknown)}, which the installed "
        f"anthropic {anthropic.__version__} does not accept. The SDK contract "
        f"changed; fix the call rather than pinning an old SDK."
    )


def test_the_arguments_the_design_depends_on_are_still_available():
    """Structured output is not optional here - without forced tool use the
    contract in models.py has nothing to validate."""
    from anthropic.resources.messages import Messages

    accepted = set(inspect.signature(Messages.create).parameters)
    for required in ("model", "max_tokens", "system", "tools", "tool_choice", "messages"):
        assert required in accepted, (
            f"anthropic {anthropic.__version__} no longer accepts {required!r}; "
            f"Stage 3 cannot force a structured response without it"
        )


def test_usage_fields_the_cost_section_reports_are_present():
    from anthropic.types import Usage

    for field in ("input_tokens", "output_tokens"):
        assert field in Usage.model_fields, (
            f"Usage.{field} is gone; tools/report.py and the cost section of the "
            f"writeup read it out of the committed cache"
        )


def test_no_stale_temperature_reference_survives_anywhere():
    """The parameter is gone from the SDK. If it creeps back into the cache key
    the committed cache silently stops matching on a reviewer's machine."""
    from pipeline import llm_cache

    assert not hasattr(cfg, "LLM_TEMPERATURE")
    key_src = inspect.getsource(llm_cache.request_key)
    assert "temperature" not in key_src


def test_requirements_pin_an_sdk_that_has_the_current_contract():
    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    line = next(l for l in text.splitlines() if l.startswith("anthropic"))
    assert ">=1.0" in line, f"requirements.txt says {line!r}; the call site needs 1.0+"
