# -*- coding: utf-8 -*-
"""R-A2/R-A3/R-A5 (mandat V2, 13/10/2026) : tests de cohérence du rendu et de
version. Chaque test cible une correction Classe A (affichage/robustesse),
aucun ne verrouille un comportement de décision."""

from __future__ import annotations

import pathlib
import re

import pytest

from bluestar import __version__
from bluestar.decide.selection_grid import (
    AssetClass,
    Decision,
    DecisionState,
    Direction,
    GRID_VERSION,
)
from bluestar.extract.desk_parser import parse_desk
from bluestar.extract.macro_parser import parse_macro
from bluestar.render.html_report import render_report


DATA = "tests/data"


def _dec(state, source_reject_code=None, asset=AssetClass.FX_PAIR):
    return Decision(
        pair="EUR/USD",
        direction=Direction.LONG,
        state=state,
        source_reject_code=source_reject_code,
        asset_class=asset,
        grid_version=GRID_VERSION,
        legs=(),
        limiting_factor="test",
        advisories=(),
    )


# ------------------------- R-A2 : libellé jambe -------------------------

def test_ra2_leg_label_reject_differs_from_single_leg():
    """Rejet desk : la colonne jambe doit citer le code de rejet et pointer
    vers l'Advisory pour l'IPS. Jambe unique : libellé 'Sans objet'."""
    html = render_report(
        desk=parse_desk(open(f"{DATA}/desk.html", encoding="utf-8").read()),
        macro=parse_macro(open(f"{DATA}/macro.html", encoding="utf-8").read()),
        decisions=(
            _dec(DecisionState.REJECT, "LOW_QUALITY"),
            _dec(DecisionState.WATCH, None, AssetClass.EQUITY_INDEX),
        ),
    )
    rej = html[html.find('<td class="verdict-cell">'):]
    assert "Non évalué par la grille — rejeté en amont (code LOW_QUALITY)" in html
    assert "Sans objet — instrument en jambe unique (biais de régime)" in html
    assert "Non applicable / non évalué" not in html


def test_ra2_leg_label_no_active_html():
    """Les libellés R-A2 passent par _esc : un code de rejet contenant du
    markup ne doit pas produire de balise active."""
    html = render_report(
        desk=parse_desk(open(f"{DATA}/desk.html", encoding="utf-8").read()),
        macro=parse_macro(open(f"{DATA}/macro.html", encoding="utf-8").read()),
        decisions=(_dec(DecisionState.REJECT, "<img src=x onerror=1>"),),
    )
    assert "<img" not in html


# ------------------------- R-A3 : échappement footer -------------------------

def _snapshots_with(payload: str):
    """Construit desk/macro dont report_datetime porte du markup actif."""
    import dataclasses
    desk = parse_desk(open(f"{DATA}/desk.html", encoding="utf-8").read())
    macro = parse_macro(open(f"{DATA}/macro.html", encoding="utf-8").read())
    desk = dataclasses.replace(desk, report_datetime=payload, report_timezone=payload)
    macro = dataclasses.replace(macro, report_datetime=payload, report_timezone=payload)
    return desk, macro


def test_ra3_footer_escapes_datetime_and_timezone():
    """R-A3 : les 4 champs du footer-note doivent être échappés."""
    desk, macro = _snapshots_with('<img src=x onerror=1>')
    html = render_report(
        desk=desk, macro=macro,
        decisions=(_dec(DecisionState.WATCH, None, AssetClass.EQUITY_INDEX),),
    )
    footer = html[html.find("footer-note"):]
    assert "<img" not in footer, "markup actif echappé dans le footer"
    assert "&lt;img" in footer, "_esc doit produire &lt;img"


def test_ra3_footer_legitimate_is_byte_identical():
    """R-A3 : un rendu légitime (valeurs sûres) est byte-identique à avant.
    La correction ne change rien quand rien n'est hostile."""
    desk, macro = _snapshots_with("2026-10-13 15:26")
    html = render_report(
        desk=desk, macro=macro,
        decisions=(_dec(DecisionState.WATCH, None, AssetClass.EQUITY_INDEX),),
    )
    footer = html[html.find("footer-note"):]
    assert "Données desk : 2026-10-13 15:26 2026-10-13 15:26" in footer
    assert "Données macro : 2026-10-13 15:26 2026-10-13 15:26" in footer


# ------------------------- R-A5 : cohérence de version -------------------------

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    txt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', txt, re.M)
    assert m, "version introuvable dans pyproject.toml"
    return m.group(1)


def test_ra5_pyproject_matches_package_version():
    """R-A5 : pyproject.toml.version == bluestar.__version__."""
    assert _pyproject_version() == __version__, (
        f"pyproject {_pyproject_version()!r} != bluestar.__version__ {__version__!r}"
    )


def test_ra5_pyproject_description_is_not_mojibake():
    """R-A4 : la description pyproject est UTF-8 propre (pas de double encodage)."""
    txt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "Ã" not in txt and "â€" not in txt


def test_ra5_grid_version_pinned():
    """R-A5 : GRID_VERSION est verrouillée à la valeur attendue."""
    assert GRID_VERSION == "bluestar-decide-v2.6"


def test_ra5_readme_present():
    """R-A4 : pyproject déclare readme = 'README.md', le fichier doit exister."""
    assert (ROOT / "README.md").is_file(), "pyproject declare readme = README.md ; absent"

# ------------------------- R-A6 : horizon arrondi -------------------------

def test_ra6_horizon_rounds_not_truncates():
    """R-A6 : un horizon de 90.8 h doit s'afficher 91 h, pas 90 h
    (int() tronque silencieusement la couverture déclarée). Note : pour la
    valeur réelle du cycle 13/10 (90.45), int() et round() donnent toutes
    deux 90 h — la correction ne change rien ce cycle, elle évite la
    sous-estimation pour les horizons à partie décimale ≥ 0.5 (ex. 90.8)."""
    from dataclasses import replace
    from bluestar.extract.desk_parser import parse_desk
    from bluestar.render.html_report import render_report
    from bluestar.models import DeskSnapshot
    desk = parse_desk(open(f"{DATA}/desk.html", encoding="utf-8").read())
    macro = parse_macro(open(f"{DATA}/macro.html", encoding="utf-8").read())
    cov = dict(desk.calendar_coverage_meta or {})
    cov["truncated"] = "true"
    cov["horizon_h"] = "90.8"
    desk = replace(desk, calendar_coverage_meta=cov)
    html = render_report(desk=desk, macro=macro, decisions=(
        Decision(pair="US30/USD", direction=None, state=DecisionState.WATCH,
                 legs=(), limiting_factor="x",
                 asset_class=AssetClass.EQUITY_INDEX),))
    assert "horizon 91 h" in html, "arrondi attendu (round), pas de troncature"
    assert "horizon 90 h" not in html
