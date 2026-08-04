"""
Tests Proposition 5 (ICF v2, rapport de synergie du 03/08/2026, C-8) :
ventilation du KPI "Advisories" en actionnables (lignes ELIGIBLE/WATCH) vs
informatives (lignes déjà BLOCKED/REJECT).

À intégrer dans tests/test_render.py.

Patch pur affichage : `_advisory_breakdown` est une fonction pure extraite
de `render_report`, ne touche à aucune décision, aucun état. Le total brut
(déjà affiché avant ce patch) est préservé à l'identique -- seule sa
ventilation est ajoutée.
"""

from __future__ import annotations

from bluestar.decide.selection_grid import AssetClass, Decision, DecisionState, Direction
from bluestar.render.html_report import _advisory_breakdown


def _decision(state: DecisionState, n_advisories: int) -> Decision:
    return Decision(
        pair="X/Y",
        direction=Direction.LONG if n_advisories else None,
        state=state,
        legs=(),
        limiting_factor="—",
        advisories=tuple(f"advisory {i}" for i in range(n_advisories)),
        asset_class=AssetClass.FX_PAIR,
    )


def test_advisory_breakdown_matches_2026_08_03_cycle_shape():
    """Reproduit la forme du cycle réel : 1 ELIGIBLE avec 2 advisories,
    2 WATCH avec 3 advisories au total, 30 BLOCKED/REJECT avec 45
    advisories informatives au total -> total 50, actionnables 5,
    informatives 45 (proportions exactes du rapport de synergie)."""
    decisions = (
        _decision(DecisionState.ELIGIBLE, 2),
        _decision(DecisionState.WATCH, 2),
        _decision(DecisionState.WATCH, 1),
        *[_decision(DecisionState.REJECT, 1) for _ in range(15)],
        *[_decision(DecisionState.BLOCKED_DATA, 2) for _ in range(15)],
    )
    total, actionable, informative = _advisory_breakdown(decisions)
    assert total == 50
    assert actionable == 5          # ELIGIBLE(2) + WATCH(2) + WATCH(1)
    assert informative == 45        # 15*1 + 15*2
    assert actionable + informative == total


def test_advisory_breakdown_zero_advisories():
    decisions = (_decision(DecisionState.ELIGIBLE, 0), _decision(DecisionState.REJECT, 0))
    assert _advisory_breakdown(decisions) == (0, 0, 0)


def test_advisory_breakdown_all_actionable():
    decisions = (_decision(DecisionState.ELIGIBLE, 3), _decision(DecisionState.WATCH, 2))
    total, actionable, informative = _advisory_breakdown(decisions)
    assert total == 5 and actionable == 5 and informative == 0


def test_advisory_breakdown_all_informative():
    decisions = (_decision(DecisionState.REJECT, 3), _decision(DecisionState.BLOCKED_RISK, 4))
    total, actionable, informative = _advisory_breakdown(decisions)
    assert total == 7 and actionable == 0 and informative == 7


def test_advisory_breakdown_never_mutates_input():
    """Zéro régression : fonction pure, les Decision passées ne sont pas
    modifiées (frozen dataclass de toute façon, mais on vérifie le
    comportement, pas juste l'immutabilité du type)."""
    decisions = (_decision(DecisionState.ELIGIBLE, 2),)
    before = decisions[0].advisories
    _advisory_breakdown(decisions)
    assert decisions[0].advisories == before
