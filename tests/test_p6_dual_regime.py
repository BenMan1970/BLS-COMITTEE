"""
Tests Proposition 6 (ICF v2, rapport de synergie du 03/08/2026, Règle
Absolue 4) : le Desk et le Macro portent chacun un "régime" -- deux
constructions différentes (état calendaire vs état de marché) -- et le
Comité doit les nommer distinctement plutôt que de n'en montrer qu'un.

À intégrer :
- la partie parser -> tests/test_extract_desk.py
- la partie rendu -> tests/test_render.py

Patch pur affichage, comme P5 : `_parse_macro_regime_label` ne conditionne
aucune décision (vérifié en Rejets Internes du rapport : injecter la force
macro dans un score serait un double comptage -- même logique ici).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from bluestar.extract.desk_parser import _parse_macro_regime_label


# ---------------------------------------------------------------------
# 1. Extraction (desk_parser) — span-frère du bandeau
# ---------------------------------------------------------------------

_SUBBAR_WITH_REGIME = """
<html><body>
<div class="page-subbar">
  <span>2026-08-03 19:02 CEST</span>
  <span>Universe 33/33</span>
  <span>Régime : POST_POLICY_REPRICING</span>
  <span>Event Risk : ÉLEVÉ</span>
</div>
</body></html>
"""

_SUBBAR_WITHOUT_ACCENT = """
<html><body>
<div class="page-subbar">
  <span>Regime : POST_POLICY_REPRICING</span>
</div>
</body></html>
"""

_SUBBAR_WITH_BADGE_BETWEEN = """
<html><body>
<div class="page-subbar">
  <span>Régime : POST_POLICY_REPRICING</span>
  <span>SR indisponible · mode ATR</span>
  <span>CONFIDENTIEL</span>
</div>
</body></html>
"""

_SUBBAR_WITHOUT_REGIME = """
<html><body>
<div class="page-subbar"><span>Universe 33/33</span></div>
</body></html>
"""


def test_parse_macro_regime_label_present():
    soup = BeautifulSoup(_SUBBAR_WITH_REGIME, "html.parser")
    assert _parse_macro_regime_label(soup) == "POST_POLICY_REPRICING"


def test_parse_macro_regime_label_tolerant_to_missing_accent():
    """Ancrage C-9 du rapport de synergie : macro_parser._parse_regime
    échoue silencieusement sans accent. On vérifie ici que la version
    Desk NE REPRODUIT PAS cette fragilité."""
    soup = BeautifulSoup(_SUBBAR_WITHOUT_ACCENT, "html.parser")
    assert _parse_macro_regime_label(soup) == "POST_POLICY_REPRICING"


def test_parse_macro_regime_label_immune_to_badge_bleed():
    """Même garde que PATCH-THEMES-BLEED : un badge intercalé entre le
    span Régime et le span CONFIDENTIEL ne doit pas fuiter dans la
    valeur extraite."""
    soup = BeautifulSoup(_SUBBAR_WITH_BADGE_BETWEEN, "html.parser")
    assert _parse_macro_regime_label(soup) == "POST_POLICY_REPRICING"
    assert "SR indisponible" not in _parse_macro_regime_label(soup)


def test_parse_macro_regime_label_absent_returns_none():
    soup = BeautifulSoup(_SUBBAR_WITHOUT_REGIME, "html.parser")
    assert _parse_macro_regime_label(soup) is None


def test_parse_macro_regime_label_no_subbar_returns_none():
    soup = BeautifulSoup("<html><body>rien</body></html>", "html.parser")
    assert _parse_macro_regime_label(soup) is None


# ---------------------------------------------------------------------
# 2. Rendu (html_report) — getattr défensif, testé indirectement.
#    Nécessite un DeskSnapshot minimal pour un test d'intégration complet
#    (non fourni -- UNKNOWN, cf. bluestar/models.py non transmis à ce
#    tour). Le comportement attendu, à vérifier une fois models.py fourni :
#    - desk.macro_regime_label absent (getattr -> None) => ligne
#      "Régime desk (état calendaire) : non disponible", zéro exception.
#    - desk.macro_regime_label = "POST_POLICY_REPRICING" => ligne
#      "Régime desk (état calendaire) : POST_POLICY_REPRICING", distincte
#      de la ligne "Régime macro (état de marché) : Mixed / Selective".
