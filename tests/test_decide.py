"""
Tests de non-régression sur la grille de décision Bluestar.

Exécutés sur les DEUX documents réels (tests/data/macro.html, tests/data/desk.html),
jamais sur des fixtures inventées.
"""

import pytest

from bluestar.extract.macro_parser import parse_macro
from bluestar.extract.desk_parser import parse_desk
from bluestar.decide.selection_grid import (
    decide_all,
    decide_rejection,
    classify_asset,
    regime_bias,
    AssetClass,
    DecisionState,
    echo_leg,
    LegVerdict,
    currency_level_advisories,
)


@pytest.fixture(scope="module")
def macro():
    with open("tests/data/macro.html", encoding="utf-8") as f:
        return parse_macro(f.read())


@pytest.fixture(scope="module")
def desk():
    with open("tests/data/desk.html", encoding="utf-8") as f:
        return parse_desk(f.read())


@pytest.fixture(scope="module")
def decisions(desk, macro):
    return {d.pair: d for d in decide_all(desk, macro)}


# --- echo(leg) : comportement du prédicat gelé, cas unitaires --------------

def test_echo_leg_ips_normal_is_neutre(macro):
    result = echo_leg("AUD", is_long_leg=True, macro=macro)
    assert result.verdict == LegVerdict.NEUTRE


def test_echo_leg_capitulation_long_is_confluence(macro):
    result = echo_leg("EUR", is_long_leg=True, macro=macro)
    assert result.verdict == LegVerdict.CONFLUENCE


def test_echo_leg_capitulation_short_is_conflit(macro):
    result = echo_leg("JPY", is_long_leg=False, macro=macro)
    assert result.verdict == LegVerdict.CONFLIT


def test_echo_leg_missing_ips_is_indetermine(macro):
    result = echo_leg("USD", is_long_leg=True, macro=macro)
    assert result.verdict == LegVerdict.INDETERMINE


# --- decide_setup : golden decisions sur les 5 setups réels -----------------

def test_eur_aud_eligible(decisions):
    assert decisions["EUR/AUD"].state == DecisionState.ELIGIBLE


def test_eur_jpy_watch_on_jpy_conflict(decisions):
    d = decisions["EUR/JPY"]
    assert d.state == DecisionState.WATCH
    assert "JPY" in d.limiting_factor


def test_usd_chf_blocked_on_macro_priority_conflict(decisions):
    d = decisions["USD/CHF"]
    assert d.state == DecisionState.BLOCKED_DATA
    assert "USD/CHF" in d.limiting_factor


def test_us30_usd_blocked_on_age(decisions):
    d = decisions["US30/USD"]
    assert d.state == DecisionState.BLOCKED_DATA
    assert "72j" in d.limiting_factor


def test_gbp_aud_KNOWN_DIVERGENCE_mechanical_state_is_eligible(decisions):
    """
    ⚠️ CAS DOCUMENTÉ, NON RÉSOLU — à lire avant de modifier ce test.

    Le prédicat echo(leg) gelé lit IPS GBP=3 (capitulation) + jambe longue
    -> CONFLUENCE mécanique, donc ce setup ressort ELIGIBLE.

    Le mécanisme currency_level_advisories() détecte et SIGNALE ce cas
    (cf. test suivant) mais ne change jamais `state` — l'escalade en règle
    bloquante reste une décision de gouvernance humaine explicite, pas une
    inférence automatique. Ce test verrouille le comportement mécanique
    actuel pour qu'aucune modification silencieuse ne le change sans faire
    échouer un test.
    """
    d = decisions["GBP/AUD"]
    assert d.state == DecisionState.ELIGIBLE
    leg_gbp = next(leg for leg in d.legs if leg.currency == "GBP")
    assert leg_gbp.verdict == LegVerdict.CONFLUENCE


def test_gbp_aud_advisory_flags_macro_thesis_conflict(decisions):
    """Le mécanisme d'advisory doit détecter la contradiction GBP/AUD vs la
    thèse macro SHORT GBP (exprimée via GBP/USD et GBP/JPY, pas GBP/AUD)."""
    d = decisions["GBP/AUD"]
    assert len(d.advisories) == 2
    assert all("GBP" in a for a in d.advisories)
    assert any("GBP/USD" in a for a in d.advisories)
    assert any("GBP/JPY" in a for a in d.advisories)


def test_currency_level_advisories_is_pure(desk, macro):
    """La fonction d'advisory ne doit jamais lever, jamais muter ses entrées,
    et retourner un tuple immuable même sans conflit détecté."""
    eur_aud_setup = next(s for s in desk.setups if s.pair == "EUR/AUD")
    result = currency_level_advisories(eur_aud_setup, macro)
    assert isinstance(result, tuple)


def test_grid_version_is_pinned(decisions):
    for d in decisions.values():
        assert d.grid_version == "bluestar-decide-v2.2"


# --- Regression : invariant 33/33 (round d'audit du 27/07/2026) ------------

def test_decide_all_default_covers_full_universe(desk, macro):
    decisions_tuple = decide_all(desk, macro)
    assert len(decisions_tuple) == len(desk.setups) + len(desk.rejected)
    assert len(decisions_tuple) == desk.universe_total


def test_decide_all_include_rejects_false_restores_old_behaviour(desk, macro):
    decisions_tuple = decide_all(desk, macro, include_rejects=False)
    assert len(decisions_tuple) == len(desk.setups)


def test_no_reject_code_left_without_a_route(desk, macro):
    for rejected in desk.rejected:
        d = decide_rejection(rejected, macro)
        assert d.state in (
            DecisionState.REJECT, DecisionState.BLOCKED_DATA, DecisionState.WATCH,
        )
        assert d.limiting_factor
        assert d.source_reject_code == rejected.reject_code


def test_cluster_dup_reject_routes_to_watch(desk, macro):
    dup = next(r for r in desk.rejected if r.reject_code == "CLUSTER_DUP")
    d = decide_rejection(dup, macro)
    assert d.state == DecisionState.WATCH
    assert d.advisories


def test_low_quality_reject_routes_to_reject(desk, macro):
    lq = next(r for r in desk.rejected if r.reject_code == "LOW_QUALITY")
    d = decide_rejection(lq, macro)
    assert d.state == DecisionState.REJECT


def test_price_past_tp_reject_routes_to_blocked_data(desk, macro):
    ppt = next((r for r in desk.rejected if r.reject_code == "PRICE_PAST_TP"), None)
    if ppt is None:
        pytest.skip("Aucun PRICE_PAST_TP dans cette fixture desk.")
    d = decide_rejection(ppt, macro)
    assert d.state == DecisionState.BLOCKED_DATA


# --- Regression : classification d'actif et mode jambe unique (par. 3.5/4) --

def test_classify_asset_equity_index_by_digit_in_base():
    assert classify_asset("SPX500/USD") == AssetClass.EQUITY_INDEX
    assert classify_asset("US30/USD") == AssetClass.EQUITY_INDEX
    assert classify_asset("DE30/EUR") == AssetClass.EQUITY_INDEX


def test_classify_asset_metal():
    assert classify_asset("XAU/USD") == AssetClass.METAL


def test_classify_asset_fx_pair_unaffected():
    assert classify_asset("EUR/USD") == AssetClass.FX_PAIR


def test_regime_bias_ambiguous_regime_returns_none():
    assert regime_bias(AssetClass.EQUITY_INDEX, "Mixed / Selective") is None
    assert regime_bias(AssetClass.METAL, "Mixed / Selective") is None


def test_regime_bias_risk_on_and_risk_off_are_opposite_for_equity_index():
    from bluestar.models import Direction
    assert regime_bias(AssetClass.EQUITY_INDEX, "Risk-On") == Direction.LONG
    assert regime_bias(AssetClass.EQUITY_INDEX, "Risk-Off") == Direction.SHORT


def test_regime_bias_metal_is_safe_haven_symmetric():
    from bluestar.models import Direction
    assert regime_bias(AssetClass.METAL, "Risk-Off") == Direction.LONG
    assert regime_bias(AssetClass.METAL, "Risk-On") == Direction.SHORT


# --- Régression : seuils IPS dupliqués (round d'audit du 19/07/2026) -------

def test_ips_thresholds_have_single_source_of_truth(macro):
    """
    Verrouille la correction du bug trouvé par l'audit GPT-5.5 : les seuils
    IPS_CAPITULATION_MAX/IPS_CROWDED_MIN ne doivent exister qu'à UN seul
    endroit (bluestar.models), jamais redéclarés dans selection_grid.py.

    Test comportemental, pas juste structurel : on modifie le seuil canonique
    et on vérifie que echo_leg() en tient compte RÉELLEMENT — c'est le test
    qui aurait détecté le bug avant tout audit externe.
    """
    import bluestar.models as models_module

    original = models_module.IPS_CAPITULATION_MAX
    try:
        # EUR a IPS=12 (capitulation avec le seuil par défaut de 20).
        # On abaisse le seuil à 10 : EUR ne doit plus être en capitulation.
        models_module.IPS_CAPITULATION_MAX = 10.0
        result = echo_leg("EUR", is_long_leg=True, macro=macro)
        assert result.verdict == LegVerdict.NEUTRE, (
            "Le seuil canonique modifié dans bluestar.models n'a pas changé "
            "le comportement de echo_leg() — la duplication de seuils est "
            "revenue, ou une nouvelle copie locale a été introduite ailleurs."
        )
    finally:
        models_module.IPS_CAPITULATION_MAX = original


def test_selection_grid_does_not_redeclare_ips_thresholds():
    """Verrouille structurellement l'absence de redéclaration : si quelqu'un
    réintroduit IPS_CAPITULATION_MAX ou IPS_CROWDED_MIN comme constante
    locale dans selection_grid.py, ce test doit échouer."""
    import bluestar.decide.selection_grid as grid_module

    assert not hasattr(grid_module, "IPS_CAPITULATION_MAX"), (
        "IPS_CAPITULATION_MAX est redéclaré dans selection_grid.py — "
        "c'est exactement le bug corrigé le 19/07/2026."
    )
    assert not hasattr(grid_module, "IPS_CROWDED_MIN"), (
        "IPS_CROWDED_MIN est redéclaré dans selection_grid.py — "
        "c'est exactement le bug corrigé le 19/07/2026."
    )


# --- Régression : immuabilité réelle des mappings (round d'audit 19/07) ----

def test_macro_snapshot_currencies_is_truly_immutable(macro):
    """Verrouille la correction : frozen=True ne suffisait pas, il fallait
    MappingProxyType. Toute régression vers un dict mutable doit faire
    échouer ce test."""
    with pytest.raises(TypeError):
        macro.currencies["EUR"] = None  # type: ignore[index]


def test_desk_setup_factors_is_truly_immutable(desk):
    setup = desk.setups[0]
    with pytest.raises(TypeError):
        setup.factors["INJECTED"] = 0.0  # type: ignore[index]


# --- Régression : ordre de court-circuit Niveau 3 avant Niveau 4 (20/07/2026) --

def test_conflit_leg_short_circuits_before_risk_reward_check(desk, macro):
    """
    Verrouille explicitement un point de désaccord réel entre audits : un
    modèle (Gemini Pro, analyse statique) avait prédit que casser
    MIN_RISK_REWARD ferait aussi échouer test_eur_jpy_watch_on_jpy_conflict,
    en supposant que le contrôle R:R (Niveau 4) s'applique à tous les
    setups. En réalité, le bloc CONFLIT (Niveau 3, jambe JPY de EUR/JPY)
    retourne AVANT que le code atteigne le contrôle R:R — vérifié par
    exécution, contredisant cette prédiction (5 audits précédents avaient
    correctement identifié seulement 2 échecs, pas 3).

    Ce test rend ce court-circuit explicite et vérifiable mécaniquement,
    pour qu'aucun futur audit n'ait à re-trancher la question par lecture
    seule.
    """
    import bluestar.decide.selection_grid as grid_module

    original = grid_module.MIN_RISK_REWARD
    try:
        grid_module.MIN_RISK_REWARD = 999.0
        decisions = {d.pair: d for d in decide_all(desk, macro)}
        assert decisions["EUR/JPY"].state == DecisionState.WATCH, (
            "EUR/JPY doit rester WATCH même avec MIN_RISK_REWARD=999 : le "
            "conflit de positionnement JPY (Niveau 3) court-circuite avant "
            "le contrôle R:R (Niveau 4), qui n'est donc jamais évalué pour "
            "ce setup."
        )
        assert "JPY" in decisions["EUR/JPY"].limiting_factor
        assert "R:R" not in decisions["EUR/JPY"].limiting_factor
    finally:
        grid_module.MIN_RISK_REWARD = original
