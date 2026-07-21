from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from bluestar.extract.macro_parser import parse_macro
from bluestar.extract.desk_parser import parse_desk
from bluestar.decide.selection_grid import decide_all, DecisionState
from bluestar.render.html_report import render_report
from bluestar.errors import RenderError


@pytest.fixture(scope="module")
def rendered():
    with open("tests/data/macro.html", encoding="utf-8") as f:
        macro = parse_macro(f.read())
    with open("tests/data/desk.html", encoding="utf-8") as f:
        desk = parse_desk(f.read())
    decisions = decide_all(desk, macro)
    html = render_report(desk, macro, decisions, generated_at=datetime(2026, 7, 18, tzinfo=timezone.utc))
    return html, desk, macro, decisions


def test_output_is_valid_html(rendered):
    html, *_ = rendered
    soup = BeautifulSoup(html, "html.parser")
    assert soup.title.get_text() == "Comité de Sélection — BLUESTAR"


def test_logo_star_is_blue_on_white_background(rendered):
    """Non-régression design : étoile bleu royal sur fond blanc, jamais l'inverse
    (erreur commise et corrigée dans une itération précédente du projet)."""
    html, *_ = rendered
    assert 'fill="#1B45B4"' in html
    assert '.logo-marker{width:42px;height:42px;border-radius:5px;background:#FFFFFF' in html


def test_row_count_matches_decisions(rendered):
    html, _, _, decisions = rendered
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find("tbody").find_all("tr")
    assert len(rows) == len(decisions)


def test_all_badges_map_to_valid_states(rendered):
    html, *_ = rendered
    soup = BeautifulSoup(html, "html.parser")
    badges = {b.get_text(strip=True) for b in soup.find_all(class_="badge")}
    valid_states = {s.value for s in DecisionState}
    assert badges.issubset(valid_states)
    assert badges  # au moins un badge présent


def test_eligible_count_in_kpi_matches_decisions(rendered):
    html, _, _, decisions = rendered
    eligible_count = sum(1 for d in decisions if d.state == DecisionState.ELIGIBLE)
    soup = BeautifulSoup(html, "html.parser")
    kpi_vals = [k.find(class_="val").get_text(strip=True) for k in soup.find_all(class_="kpi")]
    assert str(eligible_count) in kpi_vals


def test_render_report_raises_on_empty_decisions(rendered):
    _, desk, macro, _ = rendered
    with pytest.raises(RenderError):
        render_report(desk, macro, ())


def test_footer_disclaimer_present(rendered):
    """La distinction ELIGIBLE != EXECUTER doit être visible dans le rendu,
    pas seulement dans la documentation — c'est un garde-fou utilisateur."""
    html, *_ = rendered
    assert "ELIGIBLE ≠ EXECUTER" in html
