import pytest

from bluestar.extract.macro_parser import parse_macro
from bluestar.errors import MacroDocumentError


@pytest.fixture(scope="module")
def macro():
    with open("tests/data/macro.html", encoding="utf-8") as f:
        return parse_macro(f.read())


def test_report_datetime(macro):
    assert "17/07/2026" in macro.report_datetime
    assert macro.report_timezone == "CET"


def test_strength_ranking_complete(macro):
    assert len(macro.currencies) == 8
    assert macro.currencies["USD"].strength_score == 80.0
    assert macro.currencies["USD"].strength_rank == 1
    assert macro.currencies["GBP"].strength_score == 8.0
    assert macro.currencies["GBP"].strength_rank == 8


def test_ips_values(macro):
    assert macro.currencies["GBP"].ips == 3.0
    assert macro.currencies["EUR"].ips == 12.0
    assert macro.currencies["AUD"].ips == 69.0


def test_ips_missing_for_usd(macro):
    """USD n'a pas de contrat CFTC autonome — doit être None, jamais estimé."""
    assert macro.currencies["USD"].ips is None


def test_priority_setups(macro):
    pairs = {p.pair: (p.direction.value, p.conviction_stars) for p in macro.priority_setups}
    assert pairs["GBP/USD"] == ("short", 4)
    assert pairs["USD/CHF"] == ("long", 4)
    assert pairs["GBP/JPY"] == ("short", 2)


def test_malformed_document_raises_typed_error():
    with pytest.raises(MacroDocumentError):
        parse_macro("<html><body>document vide sans structure attendue</body></html>")


def test_non_numeric_strength_score_raises_typed_error_not_valueerror():
    """
    Verrouille la correction du 20/07/2026 (même classe de défaut que le
    cast R:R de desk_parser.py, trouvée par l'audit Gemini Pro sur ce champ
    précis et étendue ici par cohérence) : un score de force relative non
    numérique levait ValueError brute au lieu de MacroDocumentError
    catégorisée (code CLI 1).
    """
    from bs4 import BeautifulSoup

    with open("tests/data/macro.html", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    first_val = soup.find(class_="rank-val")
    first_val.string = "N/A"
    with pytest.raises(MacroDocumentError):
        parse_macro(str(soup))
