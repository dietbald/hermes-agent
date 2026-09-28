"""Provider-preference assembly for OpenRouter routing.

Covers the `zdr` (zero-data-retention) flag, which had plumbing in the
gateway/TUI callers but no coverage on the assembly step that actually puts
it into the request body.
"""

from types import SimpleNamespace

from agent.chat_completion_helpers import _provider_preferences_for_agent


def _agent(**overrides):
    base = dict(
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        provider_require_parameters=False,
        provider_data_collection=None,
        provider_zdr=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_zdr_absent_when_not_requested():
    assert "zdr" not in _provider_preferences_for_agent(_agent())


def test_zdr_set_when_requested():
    prefs = _provider_preferences_for_agent(_agent(provider_zdr=True))
    assert prefs["zdr"] is True


def test_zdr_ignores_truthy_non_true_values():
    """Only an explicit True enables ZDR; a stray string must not."""
    prefs = _provider_preferences_for_agent(_agent(provider_zdr="yes"))
    assert "zdr" not in prefs


def test_zdr_absent_when_attribute_missing():
    """Agents built before the flag existed must not blow up."""
    agent = _agent()
    del agent.provider_zdr
    assert "zdr" not in _provider_preferences_for_agent(agent)
