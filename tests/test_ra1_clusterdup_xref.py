# -*- coding: utf-8 -*-
"""R-A1 (mandat V2, 13/10/2026) : anti-regression de la correction A3 sur
l'XREF CLUSTER_DUP.

Contexte : la correction A3 du 13/10/2026 a fait lire a
`DeskRejectedSetup.detail` la cellule « Detail » seule au lieu du texte
complet de la ligne. Or `decide_all` extrait le representant d'un doublon
CLUSTER_DUP par re.search(r"=\s*([A-Za-z0-9]+/[A-Za-z0-9]+)", r.detail) :
si un document Desk place le « = PAIRE/PAIRE » dans une autre cellule de la
ligne, l'advisory de contre-reference disparaissait silencieusement.

Ces tests prouvent que l'XREF est produite dans les deux cas (representant
dans la cellule Detail, ou ailleurs dans la ligne). Aucun etat, aucun
compteur, aucun autre texte n'est touche.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from bluestar.decide.selection_grid import decide_all
from bluestar.extract.desk_parser import parse_desk
from bluestar.extract.macro_parser import parse_macro
from bluestar.models import DeskRejectedSetup

DATA = "tests/data"


def _load_cycle():
    desk = parse_desk(open(f"{DATA}/desk.html", encoding="utf-8").read())
    macro = parse_macro(open(f"{DATA}/macro.html", encoding="utf-8").read())
    return desk, macro


def _xref_advisories(decisions):
    return [
        a for d in decisions
        for a in d.advisories
        if "representant" in a and "present dans ce rapport" in a
    ]


def test_cluster_dup_representant_in_detail_cell():
    """Cas nominal (fixture reelle) : le « = EUR/JPY » est dans la cellule
    Detail. L'advisory de contre-reference doit etre produite."""
    desk, macro = _load_cycle()
    rej = [r for r in desk.rejected if r.reject_code == "CLUSTER_DUP"]
    assert rej, "la fixture doit contenir au moins une ligne CLUSTER_DUP"
    assert "= EUR/JPY" in rej[0].detail
    dec = decide_all(desk, macro)
    xref = _xref_advisories(dec)
    assert xref, "XREF CLUSTER_DUP attendue (representant dans la cellule Detail)"
    assert "EUR/JPY" in xref[0]


_CLUSTER_DUP_TR = (
    '<tr><td style="font-family:var(--mono);font-weight:700">NZD/JPY</td>'
    '<td><span class="dir long" style="font-size:9.5px;padding:1px 6px">Bullish</span></td>'
    '<td class="reject-code">{code}</td>'
    '<td style="font-size:10px">{detail}</td>'
    '<td style="font-family:var(--mono);font-size:10px">{rsi}</td>'
    '<td style="font-family:var(--mono);font-size:10px">6j</td>'
    '<td><span class="cal-ok" style="font-size:9.5px;padding:1px 6px">OK</span></td></tr>'
)


def _desk_with_cluster_dup(detail_cell: str, rsi_cell: str = "66.26"):
    """Reconstruit la fixture desk avec une ligne CLUSTER_DUP. Le representant
    peut etre place dans la cellule Detail (rsi_cell="66.26") ou ailleurs dans
    la ligne (ex. cellule RSI), selon le document Desk."""
    src = open(f"{DATA}/desk.html", encoding="utf-8").read()
    original = _CLUSTER_DUP_TR.format(code="CLUSTER_DUP", rsi="66.26",
                                      detail="Représentant cluster JPY_weak = EUR/JPY")
    modified = _CLUSTER_DUP_TR.format(code="CLUSTER_DUP", rsi=rsi_cell,
                                      detail=detail_cell)
    assert original in src, "ligne CLUSTER_DUP d'origine introuvable dans la fixture"
    return parse_desk(src.replace(original, modified))


def _macro_of_cycle():
    return parse_macro(open(f"{DATA}/macro.html", encoding="utf-8").read())


def test_cluster_dup_representant_outside_detail_cell():
    """Regression demontree avant R-A1 : le « = EUR/JPY » est place dans la
    cellule du code de rejet, pas dans la cellule Detail. L'XREF doit quand
    meme etre produite (repli sur le texte brut de la ligne)."""
    desk = _desk_with_cluster_dup(
        detail_cell="Représentant cluster JPY_weak",
        rsi_cell="rsi = EUR/JPY",
    )
    rej = [r for r in desk.rejected if r.reject_code == "CLUSTER_DUP"]
    assert rej
    assert "= EUR/JPY" not in rej[0].detail
    dec = decide_all(desk, _macro_of_cycle())
    xref = _xref_advisories(dec)
    assert xref, (
        "XREF CLUSTER_DUP perdue : le representant est hors de la cellule Detail, "
        "le repli sur le texte brut de la ligne doit le retrouver"
    )
    assert "EUR/JPY" in xref[0]


def test_cluster_dup_representant_absent_stays_silent():
    """Aucun representant nulle part dans la ligne : aucune XRF fabrique.
    Garde anti-hallucination : la correction ne doit rien inventer."""
    desk = _desk_with_cluster_dup(
        detail_cell="Représentant cluster JPY_weak",
    )
    dec = decide_all(desk, _macro_of_cycle())
    assert not _xref_advisories(dec), "aucun « = PAIRE » dans la ligne : XREF interdite"


def test_row_text_is_never_rendered():
    """`row_text` est un champ de repli : il ne doit avoir aucun effet sur le
    facteur limitant affiche (ni sur aucun rendu)."""
    desk, _ = _load_cycle()
    for r in desk.rejected:
        assert r.pair not in r.detail
        assert r.pair in r.row_text  # row_text = ligne complete
    assert any(r.detail != r.row_text for r in desk.rejected)


def test_rejected_setup_row_text_default_empty():
    """Les constructions manuelles (tests, API) ne cassent pas : defaut vide."""
    r = DeskRejectedSetup(pair="EUR/USD", direction=None,
                          reject_code="LOW_QUALITY", detail="Quality B+")
    assert r.row_text == ""