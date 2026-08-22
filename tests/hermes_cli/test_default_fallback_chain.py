"""The shipped default fallback chain.

LuminaVault provisions one Hermes per tenant, each with a fresh PVC seeded from
``DEFAULT_CONFIG``. An empty ``fallback_providers`` meant a tenant whose primary
provider lost auth had nowhere to go and the turn simply failed. The default now
carries a zero-cost OpenRouter chain so a fresh tenant always has somewhere to
land.
"""

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.fallback_config import get_fallback_chain


def test_default_config_ships_a_non_empty_fallback_chain():
    assert DEFAULT_CONFIG["fallback_providers"], "a fresh tenant must inherit a fallback chain"


def test_default_chain_parses_into_ordered_provider_model_entries():
    chain = get_fallback_chain(DEFAULT_CONFIG)
    assert [(e["provider"], e["model"]) for e in chain] == [
        ("openrouter", "z-ai/glm-5.2:free"),
        ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
        ("openrouter", "deepseek/deepseek-v4-flash"),
    ]


def test_default_chain_leads_with_zero_cost_slugs():
    """The two ``:free`` slugs come before the billable one, so a fallback only
    starts costing money after both zero-cost hops are exhausted."""
    chain = get_fallback_chain(DEFAULT_CONFIG)
    free = [e["model"].endswith(":free") for e in chain]
    assert free == sorted(free, reverse=True), f"billable entry ordered before a free one: {chain}"
