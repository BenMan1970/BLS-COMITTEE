"""Tests de non-regression des correctifs classe A de l'audit institutionnel
du 13/10/2026 (mandat ZERO REGRESSION / ZERO HALLUCINATION).

Chaque correction atomique a son test dedie. Tous les tests tournent sur les
DEUX documents source reels (tests/data/), aucune donnee inventee.

Correctifs couverts :
  - test_p3_3 : P3-3 niveaux Entry/SL/TP1/TP2/R:R rendus (_levels_html)
  - test_p3_5 : P3-5 cardmeta conviction/quality/MTF/age/cluster (_card_meta_html)
  - test_h5   : H5 lecture de la cellule « Detail » pour le facteur limitant des rejets
  - test_h2   : H2 un seul renderer canonique (plus de doublon racine)
  - test_h3   : H3 version du package synchronisee (pyproject == __init__)
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from bluestar.extract.macro_parser import parse_macro
from bluestar.extract.desk_parser import parse_desk
from bluestar.decide.selection_grid import decide_all
from bluestar.render.html_report import render_report, _levels_html, _card_meta_html
from bluestar import __version__ as running_version


_HERE = pathlib.Path(__file__).resolve().parent
_FROZEN = datetime(2026, 10, 13, 13, 36, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def macro():
    with open(_HERE / "data" / "macro.html", encoding="utf-8") as f:
        return parse_macro(f.read())


@pytest.fixture(scope="module")
def desk():
    with open(_HERE / "data" / "desk.html", encoding="utf-8") as f:
        return parse_desk(f.read())


@pytest.fixture(scope="module")
def decisions(desk, macro):
    return decide_all(desk, macro)


# --- P3-3 : niveaux executables --------------------------------------------

def test_levels_html_empty_when_no_setup_card():
    """Un setup None (ligne rejetee, pas de carte desk) ne produit rien -
    jamais de niveaux inventes pour une ligne sans prix publiés."""
    assert _levels_html(None) == ""


def test_levels_html_renders_all_five_levels_from_real_setup(desk):
    """Sur le document desk reel, le setup valide porte les 5 champs P3-3."""
    html = _levels_html(desk.setups[0])
    assert 'class="levels"' in html
    for token in ("Entrée", "SL", "TP1", "TP2", "R:R"):
        assert token in html
    assert "None" not in html  # le contrat P3-3 : tiret, jamais "None"


def test_levels_html_absent_for_card_without_prices():
    """Une carte sans AUCUN niveau chiffre produit une ligne vide, pas une
    ligne "- · - · -" bruyante."""

    class _Bare:
        entry = stop_loss = take_profit_1 = take_profit_2 = risk_reward = None
        entry_type = None

    assert _levels_html(_Bare()) == ""


def test_levels_html_escapes_entry_type(desk):
    """entry_type vient du document (input non fiable) : il doit etre echappe."""

    class _Card:
        entry = 1.0
        stop_loss = 0.9
        take_profit_1 = take_profit_2 = risk_reward = None
        entry_type = '<img src=x onerror="alert(1)">'

    out = _levels_html(_Card())
    assert "<img" not in out
    assert "&lt;img" in out


def test_report_renders_levels_end_to_end(desk, macro, decisions):
    """Bout-en-bout : le rapport genere contient la div .levels. C'est le
    verrou du defaut H2/P3-3 (renderer deploye sans niveaux)."""
    html = render_report(desk, macro, decisions, generated_at=_FROZEN)
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("div", class_="levels") is not None


# --- P3-5 : cardmeta ---------------------------------------------------------

def test_card_meta_html_renders_real_setup(desk):
    html = _card_meta_html(desk.setups[0])
    assert 'class="cardmeta"' in html
    for token in ("Conviction", "Qualité", "MTF", "Cluster"):
        assert token in html
    assert "None" not in html


def test_card_meta_html_empty_without_card():
    assert _card_meta_html(None) == ""


def test_card_meta_absent_for_bare_card():
    class _Bare:
        conviction_grade = conviction_value = None
        quality = mtf_pct = age_days = cluster_tag = None

    assert _card_meta_html(_Bare()) == ""


# --- H5 : cellule « Detail » lue pour le facteur limitant des rejets --------

def test_rejection_detail_reads_detail_cell_not_raw_row():
    """Le facteur limitant d'un rejet doit provenir de la cellule Detail,
    PAS du get_text brut de la ligne (qui repete paire/direction/code et
    laisse le RSI sans libelle). Contrat H5 : seul le texte change, jamais
    reject_code, direction, ni l'etat."""
    from bluestar.extract.desk_parser import _parse_rejected_table

    html = """<html><body><table>
      <tr><td>NZD/USD</td><td><span class="dir short">Bearish</span></td>
          <td class="reject-code">LOW_QUALITY</td>
          <td>Quality B+</td></tr>
    </table></body></html>"""
    rows = _parse_rejected_table(BeautifulSoup(html, "html.parser"))
    assert len(rows) == 1
    r = rows[0]
    assert r.pair == "NZD/USD"
    assert r.reject_code == "LOW_QUALITY"
    assert r.detail == "Quality B+"
    assert "NZD/USD" not in r.detail
    assert "LOW_QUALITY" not in r.detail


def test_rejection_detail_falls_back_to_row_text_without_detail_cell():
    """Cellule Detail absente (document ancien / malforme) : repli sur le
    comportement d'origine - aucune information perdue, zero regression."""
    from bluestar.extract.desk_parser import _parse_rejected_table

    html = """<html><body><table>
      <tr><td>NZD/USD</td><td><span class="dir short">Bearish</span></td>
          <td class="reject-code">LOW_QUALITY</td></tr>
    </table></body></html>"""
    rows = _parse_rejected_table(BeautifulSoup(html, "html.parser"))
    assert len(rows) == 1
    assert rows[0].pair == "NZD/USD"
    assert rows[0].reject_code == "LOW_QUALITY"
    assert rows[0].detail  # non vide : repli sur la ligne complete


def test_rejection_detail_preserves_cluster_dup_consumer():
    """Le motif CLUSTER_DUP consomme par selection_grid attend un
    « = paire » dans le detail ; la lecture de la cellule Detail doit le
    preserver (regression impossible)."""
    from bluestar.extract.desk_parser import _parse_rejected_table

    html = """<html><body><table>
      <tr><td>EUR/USD</td><td><span class="dir long">Bullish</span></td>
          <td class="reject-code">CLUSTER_DUP</td>
          <td>doublon de cluster = EUR/USD (deja valide)</td></tr>
    </table></body></html>"""
    rows = _parse_rejected_table(BeautifulSoup(html, "html.parser"))
    assert rows[0].reject_code == "CLUSTER_DUP"
    assert "= EUR/USD" in rows[0].detail


def test_real_rejected_details_are_cleaned(desk):
    """Sur le document desk reel : aucun detail de rejet ne contient la
    repetition « paire | direction | code » du round pre-audit."""
    for r in desk.rejected:
        assert r.pair not in r.detail, f"{r.pair}: paire encore repetee"
        assert r.reject_code not in r.detail, f"{r.pair}: code encore repete"
        assert r.detail.strip(), f"{r.pair}: detail vide"


# --- H2 : un seul renderer canonique ---------------------------------------

def test_no_duplicate_renderer_at_repo_root():
    """Verrou H2 : le fichier html_report.py a la racine du depot (doublon
    non importe, source du defaut « renderer deploye plus ancien ») ne doit
    plus exister."""
    duplicate = _HERE.parent / "html_report.py"
    assert not duplicate.exists(), (
        "Le doublon racine html_report.py est de retour - deux versions du "
        "renderer dans le depot, c'est exactement le defaut H2."
    )


def test_render_imports_resolve_to_canonical_module():
    """bluestar.render.html_report doit etre le module canonique sous src/."""
    import bluestar.render.html_report as hr
    flat = hr.__file__.replace("\\", "/")
    assert "/src/" in flat
    assert flat.endswith("render/html_report.py")


# --- H3 : version du package synchronisee ----------------------------------

def test_running_version_matches_pyproject():
    """Verrou H3 : __version__ (rendue dans le pied de page) doit valoir la
    version declaree par pyproject.toml - sinon le pied de page ment sur la
    version reellement deployee."""
    pyproject = (_HERE.parent / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert m, "version introuvable dans pyproject.toml"
    assert running_version == m.group(1), (
        f"__version__={running_version!r} != pyproject {m.group(1)!r}"
    )
    assert running_version != "3.0.8", (
        "La version 3.0.8 du pied de page d'origine est de retour - le "
        "desynchronisme H3 aussi."
    )


def test_grid_version_is_still_pinned_v2_6(decisions):
    """AUCUN correctif classe A ne touche a la grille : GRID_VERSION doit
    rester v2.6 et le verrou doit rester vert."""
    for d in decisions:
        assert d.grid_version == "bluestar-decide-v2.6"