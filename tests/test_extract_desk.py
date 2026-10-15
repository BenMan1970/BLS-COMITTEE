import pytest

from bluestar.extract.desk_parser import parse_desk
from bluestar.errors import DeskDocumentError
from bluestar.models import Direction


@pytest.fixture(scope="module")
def desk():
    with open("tests/data/desk.html", encoding="utf-8") as f:
        return parse_desk(f.read())


def test_report_datetime(desk):
    assert desk.report_datetime == "2026-07-17 22:40"
    assert desk.report_timezone == "GMT+1"


def test_universe(desk):
    assert desk.universe_evaluated == 11
    assert desk.universe_total == 33


def test_setup_count_and_consistency(desk):
    assert len(desk.setups) == 5
    assert len(desk.rejected) == 28
    assert len(desk.setups) + len(desk.rejected) == desk.universe_total


def test_eur_jpy_setup_detail(desk):
    s = next(s for s in desk.setups if s.pair == "EUR/JPY")
    assert s.direction.value == "long"
    assert s.conviction_grade == "BBB"
    assert s.conviction_value == pytest.approx(0.77)
    assert s.quality == "A+"
    assert s.mtf_pct == pytest.approx(91.0)
    assert s.age_days == 3
    assert s.leg_currencies() == ("EUR", "JPY")


def test_usd_chf_direction_is_short(desk):
    """Le desk est SHORT USD/CHF — c'est le point de contradiction inter-sources
    central de tout l'audit. Un régresseur silencieux ici serait critique."""
    s = next(s for s in desk.setups if s.pair == "USD/CHF")
    assert s.direction.value == "short"


def test_us30_usd_has_no_fx_legs(desk):
    """US30/USD n'est pas une paire FX à deux devises malgré le '/' dans le
    symbole — leg_currencies() doit retourner None, pas ('US30', 'USD')."""
    s = next(s for s in desk.setups if s.pair == "US30/USD")
    assert s.leg_currencies() is None


def test_malformed_document_raises_typed_error():
    with pytest.raises(DeskDocumentError):
        parse_desk("<html><body>document vide sans structure attendue</body></html>")


# --- Régression : setup malformé mal catégorisé en code 5 (round 19/07/2026) --

def _desk_html_with_setup_missing(missing_class: str) -> str:
    """Construit un desk.html minimal valide, structurellement identique à un
    vrai document, mais avec UN élément requis retiré du bloc .setup."""
    fields = {
        "pair": '<span class="pair">EUR/JPY</span>',
        "dir": '<span class="dir long">Bullish</span>',
        "conv": '<span class="conv">BBB (0.77)</span>',
    }
    fields.pop(missing_class)
    inner = "".join(fields.values())
    return f"""<html><body>
    <div class="page-subbar">2026-07-17 22:40 GMT+1 Universe 1/1 Event Risk: Low Thèmes: Test</div>
    <div class="setup">{inner}</div>
    </body></html>"""


@pytest.mark.parametrize("missing_class", ["pair", "dir", "conv"])
def test_setup_missing_required_field_raises_typed_error_not_attributeerror(missing_class):
    """
    Verrouille la correction du 19/07/2026 (trouvé par l'audit GLM, confirmé
    par exécution) : un bloc .setup structurellement incomplet levait
    AttributeError, rattrapée par le filet générique de cli.py et catégorisée
    en code 5 ("erreur inattendue") — alors que c'est un problème de données
    catégorisable (DeskDocumentError, code 2).

    Ce test vérifie directement au niveau du parseur, pas seulement via la
    CLI, pour que l'échec pointe précisément vers la fonction en cause.
    """
    html = _desk_html_with_setup_missing(missing_class)
    with pytest.raises(DeskDocumentError):
        parse_desk(html)


# --- Régression : classe .dir invalide acceptée silencieusement (round 21/07/2026) --

def _desk_html_with_dir_class(dir_class_suffix: str) -> str:
    """Desk.html minimal valide, avec une classe .dir personnalisée
    (ex. 'neutral' pour simuler une valeur invalide/corrompue)."""
    return f"""<html><body>
    <div class="page-subbar">2026-07-17 22:40 GMT+1 Universe 1/1 Event Risk: Low Thèmes: Test</div>
    <div class="setup">
      <span class="pair">EUR/JPY</span>
      <span class="dir {dir_class_suffix}">Texte</span>
      <span class="conv">BBB (0.77)</span>
    </div>
    </body></html>"""


@pytest.mark.parametrize("invalid_suffix", ["neutral", "unknown", "lng", "shortish"])
def test_setup_invalid_dir_class_raises_typed_error_not_silent_short(invalid_suffix):
    """
    Verrouille la correction du 21/07/2026 (trouvé par un audit qui a
    spécifiquement testé une classe .dir PRÉSENTE mais INVALIDE — les rounds
    précédents ne testaient que .dir ABSENT — confirmé par exécution avant
    correction).

    AVANT : toute classe .dir ne contenant pas "long" devenait
    silencieusement Direction.SHORT (`"long" in classes else SHORT`). Un
    document corrompu produisait un rapport valide avec une direction
    potentiellement inversée, sans jamais lever d'erreur — corruption
    sémantique silencieuse, pas une exception catégorisable. C'est la
    trouvaille la plus sérieuse de tous les rounds d'audit à ce jour :
    aucune des trouvailles précédentes ne laissait passer une donnée
    fausse jusqu'au rapport final.
    """
    html = _desk_html_with_dir_class(invalid_suffix)
    with pytest.raises(DeskDocumentError):
        parse_desk(html)


def test_setup_dir_class_long_still_parses_correctly():
    """Garde-fou inverse : la classe valide 'long' continue de fonctionner
    après le durcissement de la validation."""
    html = _desk_html_with_dir_class("long")
    desk = parse_desk(html)
    assert desk.setups[0].direction == Direction.LONG


def test_setup_dir_class_short_still_parses_correctly():
    """Garde-fou inverse : la classe valide 'short' continue de fonctionner
    après le durcissement de la validation."""
    html = _desk_html_with_dir_class("short")
    desk = parse_desk(html)
    assert desk.setups[0].direction == Direction.SHORT


# --- Régression : metrics-grid incomplète non protégée (round 21/07/2026) --

def test_setup_incomplete_metrics_grid_does_not_crash():
    """
    Verrouille la correction du 21/07/2026 (trouvé par un audit docx,
    confirmé par exécution) : `_extract_metrics` accédait directement
    `.find(...).get_text()` sans garde, contrairement aux deux fonctions
    sœurs `_extract_factors` et `_extract_prices` qui avaient déjà la garde
    équivalente. Un bloc `.metric` avec `.metric-lbl` mais sans
    `.metric-val` (ou l'inverse) levait AttributeError -> code CLI 5 au lieu
    de 2. Ce test vérifie l'absence de crash ; le test CLI dédié vérifie le
    code de sortie correct bout en bout.
    """
    html = """<html><body>
    <div class="page-subbar">2026-07-17 22:40 GMT+1 Universe 1/1 Event Risk: Low Thèmes: Test</div>
    <div class="setup">
      <span class="pair">EUR/JPY</span>
      <span class="dir long">Bullish</span>
      <span class="conv">BBB (0.77)</span>
      <div class="metrics-grid">
        <div class="metric"><span class="metric-lbl">Quality</span></div>
      </div>
    </div>
    </body></html>"""
    desk = parse_desk(html)  # ne doit pas lever
    assert desk.setups[0].quality is None
    assert desk.setups[0].mtf_pct is None
    assert desk.setups[0].age_days is None


# --- Régression : casts float() non protégés sur px-grid (round 20/07/2026) --

def _desk_html_with_bad_numeric_field(field_class: str, bad_value: str) -> str:
    """Desk.html minimal valide, avec UNE valeur non numérique injectée dans
    un champ du bloc px-grid (entry, sl, ou rr)."""
    px_fields = {
        "entry": '<div class="entry"><span class="px-val">1.0</span></div>',
        "sl": '<div class="sl"><span class="px-val">1.0</span></div>',
        "rr": '<div class="rr"><span class="px-val">1.6</span></div>',
    }
    px_fields[field_class] = (
        f'<div class="{field_class}"><span class="px-val">{bad_value}</span></div>'
    )
    px_grid = f'<div class="px-grid">{"".join(px_fields.values())}</div>'
    return f"""<html><body>
    <div class="page-subbar">2026-07-17 22:40 GMT+1 Universe 1/1 Event Risk: Low Thèmes: Test</div>
    <div class="setup">
      <span class="pair">EUR/JPY</span>
      <span class="dir long">Bullish</span>
      <span class="conv">BBB (0.77)</span>
      {px_grid}
    </div>
    </body></html>"""


@pytest.mark.parametrize("field_class", ["entry", "sl", "rr"])
def test_setup_non_numeric_price_field_raises_typed_error_not_valueerror(field_class):
    """
    Verrouille la correction du 20/07/2026 (trouvé par l'audit Gemini Pro,
    confirmé par exécution : injecter "N/A" dans le champ R:R d'un vrai
    desk.html provoque exactement ce comportement avant correction). Un
    champ non numérique dans px-grid (entry, sl, rr) levait ValueError brute,
    rattrapée par le filet générique de cli.py et catégorisée en code 5,
    alors que c'est un document malformé catégorisable (DeskDocumentError,
    code 2) — troisième occurrence de la même classe de défaut après le
    .pair manquant (2.1.1) et l'incohérence de code 2/7 (2.1.2).
    """
    html = _desk_html_with_bad_numeric_field(field_class, "N/A")
    with pytest.raises(DeskDocumentError):
        parse_desk(html)
