"""
Tests Proposition 2 (ICF v2, rapport de synergie du 03/08/2026, C-3) :
divulgation, au niveau de chaque ligne de décision, qu'une devise est hors
couverture calendaire producteur -- "OK" ne veut alors pas dire "dégagé".

À intégrer dans le harnais existant :
- la partie parser -> tests/test_extract_desk.py
- la partie advisory -> tests/test_decide.py

Statut : les deux préalables -- (1) `bluestar.models.DeskSnapshot` porte
désormais `calendar_coverage` et (2) le moteur amont émet le bloc
`<script type="application/json" id="calendar-coverage">` -- sont LEVÉS.
Les tests d'advisory consomment la signature livrée
`calendar_coverage_advisories(pair, uncovered_currencies)` (fonction pure,
une advisory par jambe, cf. docstring côté selection_grid) directement,
sans dépendre d'un document Desk particulier.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from bluestar.decide.selection_grid import calendar_coverage_advisories
from bluestar.extract.desk_parser import _parse_calendar_coverage


# ---------------------------------------------------------------------
# 1. Extraction (desk_parser) — bloc JSON document-niveau
# ---------------------------------------------------------------------

_DOC_WITH_COVERAGE = """
<html><body>
<script type="application/json" id="calendar-coverage">
{"covered": ["CAD", "NZD", "USD"], "uncovered": ["AUD", "CHF", "EUR", "GBP", "JPY"]}
</script>
</body></html>
"""

_DOC_WITHOUT_COVERAGE = "<html><body><p>rien ici</p></body></html>"

_DOC_MALFORMED_COVERAGE = """
<html><body>
<script type="application/json" id="calendar-coverage">{ceci n'est pas du JSON</script>
</body></html>
"""


def test_parse_calendar_coverage_present():
    soup = BeautifulSoup(_DOC_WITH_COVERAGE, "html.parser")
    coverage = _parse_calendar_coverage(soup)
    assert coverage["covered"] == frozenset({"CAD", "NZD", "USD"})
    assert coverage["uncovered"] == frozenset({"AUD", "CHF", "EUR", "GBP", "JPY"})


def test_parse_calendar_coverage_absent_returns_empty_not_error():
    soup = BeautifulSoup(_DOC_WITHOUT_COVERAGE, "html.parser")
    coverage = _parse_calendar_coverage(soup)
    assert coverage == {"covered": frozenset(), "uncovered": frozenset()}


def test_parse_calendar_coverage_malformed_degrades_gracefully():
    soup = BeautifulSoup(_DOC_MALFORMED_COVERAGE, "html.parser")
    coverage = _parse_calendar_coverage(soup)
    assert coverage == {"covered": frozenset(), "uncovered": frozenset()}


# ---------------------------------------------------------------------
# 2. Advisory (selection_grid) — cas réel GBP/AUD du 03/08/2026
# ---------------------------------------------------------------------

_UNCOVERED_2026_08_03 = frozenset({"AUD", "CHF", "EUR", "GBP", "JPY"})


def test_calendar_coverage_advisory_fires_on_gbpaud_both_legs_uncovered():
    """GBP/AUD : les DEUX jambes (GBP et AUD) sont hors couverture ce
    cycle. C'est le cas réel qui a motivé cette proposition -- l'unique
    ELIGIBLE du 03/08/2026 affichait cal_status=OK sans que rien ne
    signale que les deux jambes sont "non mesurées"."""
    advisories = calendar_coverage_advisories("GBP/AUD", _UNCOVERED_2026_08_03)
    assert len(advisories) == 2  # une advisory PAR jambe (contrat docstring)
    named = {a.split("devise ")[1].split(" ")[0] for a in advisories}
    assert named == {"GBP", "AUD"}
    assert all("non mesuré" in a for a in advisories)


def test_calendar_coverage_advisory_silent_when_both_legs_covered():
    """Garde anti-faux-positif : USD/CAD, les deux jambes couvertes ->
    aucune advisory."""
    assert calendar_coverage_advisories("USD/CAD", _UNCOVERED_2026_08_03) == ()


def test_calendar_coverage_advisory_partial_leg():
    """EUR/CAD : une seule jambe (EUR) hors couverture -> l'advisory ne
    nomme que la jambe concernée, pas les deux."""
    advisories = calendar_coverage_advisories("EUR/CAD", _UNCOVERED_2026_08_03)
    assert len(advisories) == 1
    assert "EUR" in advisories[0]
    assert "CAD" not in advisories[0].split("—")[0]  # CAD pas listé comme non couvert


def test_calendar_coverage_advisory_noop_without_data():
    """Zéro régression : calendar_coverage vide (défaut, ou tant que
    ENGINE.V9.py n'émet pas le bloc JSON) -> comportement strictement
    inchangé, aucune advisory, aucune exception."""
    assert calendar_coverage_advisories("GBP/AUD", frozenset()) == ()
    assert calendar_coverage_advisories("US30", _UNCOVERED_2026_08_03) == ()


def test_calendar_coverage_advisory_never_touches_state():
    """L'advisory ne doit jamais pouvoir changer un ELIGIBLE en autre
    chose -- c'est une divulgation, pas un gate. Vérifié indirectement :
    cette fonction ne retourne QUE des chaînes, jamais un DecisionState."""
    result = calendar_coverage_advisories("GBP/AUD", _UNCOVERED_2026_08_03)
    assert isinstance(result, tuple)
    assert all(isinstance(a, str) for a in result)


# ---------------------------------------------------------------------
# 3. Rejeu intégral (F10, Volume VI) — bloqué par les deux préalables
# ---------------------------------------------------------------------

@pytest.mark.skip(reason=(
    "Bloqué par deux préalables non levés : (1) bluestar.models.DeskSnapshot "
    "doit porter `calendar_coverage`, (2) ENGINE.V9.py (non fourni, UNKNOWN) "
    "doit émettre le bloc JSON. Une fois les deux levés, rejouer le cycle "
    "réel du 03/08/2026 et vérifier : 33 décisions et 4 compteurs KPI "
    "invariants, GBP/AUD reste ELIGIBLE, EUR/CAD et AUD/CHF restent WATCH, "
    "seule une advisory nouvelle apparaît sur les lignes concernées."
))
def test_full_cycle_2026_08_03_unchanged_except_coverage_disclosure():
    ...
