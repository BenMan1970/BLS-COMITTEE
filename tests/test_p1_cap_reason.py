"""
Tests Proposition 1 (ICF v2, rapport de synergie du 03/08/2026, C-05) :
divulgation du plafond de conviction Desk (`cap_reason`) au Comité.

À intégrer dans le harnais existant :
- la partie parser -> tests/test_extract_desk.py
- la partie décision -> tests/test_decide.py

Ces tests sont écrits pour être exécutables tels quels UNE FOIS que
`bluestar.models.DeskSetup` porte le champ `cap_reason: str | None = None`
(cf. commentaire PATCH-CAPREASON dans desk_parser.py et selection_grid.py).
Avant l'ajout du champ au modèle, `test_parser_extracts_cap_reason` et
`test_augment_limiting_factor_injects_cap_reason` doivent être SKIP, pas
FAIL — la dégradation est délibérée et déjà journalisée (palier 1/2 de
_parse_setup), pas une régression.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from bluestar.decide.selection_grid import (
    Decision,
    DecisionState,
    _augment_limiting_factor_with_cap_reason,
)
from bluestar.extract.desk_parser import _extract_cap_reason
from bluestar.models import DeskSetup, Direction


# ---------------------------------------------------------------------
# Fixture F4 (Volume VI) — deux variantes minimales du bloc setup Desk
# ---------------------------------------------------------------------

_SETUP_BLOCK_WITH_CAP = """
<div class="setup">
  <div class="px-grid">
    <div class="px-card entry"><div class="px-val">1.61582</div></div>
  </div>
  <div class="cap-note">Plafond conviction appliqué : risque macro NON
  ÉVALUÉ (couverture calendaire insuffisante) — cap prudentiel</div>
  <div class="rationale">Score absolu 0.70</div>
</div>
"""

_SETUP_BLOCK_WITHOUT_CAP = """
<div class="setup">
  <div class="px-grid">
    <div class="px-card entry"><div class="px-val">1.61582</div></div>
  </div>
  <div class="rationale">Score absolu 0.70</div>
</div>
"""


def _block(html: str):
    return BeautifulSoup(html, "html.parser").find(class_="setup")


# ---------------------------------------------------------------------
# 1. Extraction (desk_parser)
# ---------------------------------------------------------------------

def test_extract_cap_reason_present():
    reason = _extract_cap_reason(_block(_SETUP_BLOCK_WITH_CAP))
    assert reason == (
        "Plafond conviction appliqué : risque macro NON ÉVALUÉ "
        "(couverture calendaire insuffisante) — cap prudentiel"
    )


def test_extract_cap_reason_absent_returns_none():
    """Garde anti-faux-positif : un setup non plafonné ne doit jamais se
    voir attribuer un cap_reason fantôme."""
    assert _extract_cap_reason(_block(_SETUP_BLOCK_WITHOUT_CAP)) is None


# ---------------------------------------------------------------------
# 2. Injection dans le facteur limitant (selection_grid)
# ---------------------------------------------------------------------

def _eligible_decision(limiting_factor: str = "—") -> Decision:
    return Decision(
        pair="GBP/AUD",
        direction=Direction.LONG,
        state=DecisionState.ELIGIBLE,
        legs=(),
        limiting_factor=limiting_factor,
    )


def test_augment_limiting_factor_injects_cap_reason():
    """Cas réel du 03/08/2026 : GBP/AUD ELIGIBLE, limiting_factor="—",
    setup plafonné par le Desk. Après patch, le motif doit apparaître et
    remplacer le placeholder "—" plutôt que le concaténer."""
    setup = DeskSetup(
        pair="GBP/AUD", direction=Direction.LONG,
        conviction_grade="A", conviction_value=0.68,
        cap_reason="risque macro NON ÉVALUÉ (couverture calendaire insuffisante)",
    )
    decision = _augment_limiting_factor_with_cap_reason(_eligible_decision(), setup)

    assert decision.state == DecisionState.ELIGIBLE  # jamais touché
    assert "—" not in decision.limiting_factor
    assert "risque macro NON ÉVALUÉ" in decision.limiting_factor


def test_augment_limiting_factor_noop_without_cap_reason_field():
    """Garantie de non-régression Zéro Régression : si bluestar.models.
    DeskSetup ne porte pas (encore) `cap_reason`, getattr renvoie None et
    la Decision ressort BYTE POUR BYTE identique."""

    class _LegacyDeskSetup:  # simule un DeskSetup pré-patch, sans cap_reason
        pair = "GBP/AUD"

    original = _eligible_decision()
    decision = _augment_limiting_factor_with_cap_reason(original, _LegacyDeskSetup())
    assert decision == original


def test_augment_limiting_factor_idempotent():
    """Un second passage sur une Decision déjà augmentée ne duplique rien."""
    setup = DeskSetup(
        pair="GBP/AUD", direction=Direction.LONG,
        conviction_grade="A", conviction_value=0.68,
        cap_reason="risque macro NON ÉVALUÉ",
    )
    once = _augment_limiting_factor_with_cap_reason(_eligible_decision(), setup)
    twice = _augment_limiting_factor_with_cap_reason(once, setup)
    assert once == twice


def test_augment_limiting_factor_preserves_real_limiting_factor():
    """Cas WATCH avec un vrai limiting_factor non-placeholder : le cap
    doit s'ajouter, pas remplacer une information existante (contrairement
    au cas "—")."""
    setup = DeskSetup(
        pair="EUR/CAD", direction=Direction.LONG,
        conviction_grade="AA", conviction_value=0.68,
        cap_reason="risque macro NON ÉVALUÉ (couverture calendaire insuffisante)",
    )
    base = Decision(
        pair="EUR/CAD", direction=Direction.LONG, state=DecisionState.WATCH,
        legs=(), limiting_factor="conflit de positionnement sur la jambe CAD",
    )
    decision = _augment_limiting_factor_with_cap_reason(base, setup)
    assert "conflit de positionnement sur la jambe CAD" in decision.limiting_factor
    assert "risque macro NON ÉVALUÉ" in decision.limiting_factor


# ---------------------------------------------------------------------
# 3. Rejeu intégral (F10, Volume VI) — à activer une fois tests/data/
#    du cycle 03/08/2026 disponible côté Comité.
# ---------------------------------------------------------------------

@pytest.mark.skip(reason=(
    "Nécessite les 3 HTML réels du 03/08/2026 en fixture + confirmation "
    "que les 33 décisions, les 4 compteurs KPI et les 50 advisories "
    "restent invariants après ce patch (F10, Volume VI) — prérequis posé "
    "par le rapport de synergie, pas encore levé."
))
def test_full_cycle_2026_08_03_unchanged_except_cap_disclosure():
    ...
