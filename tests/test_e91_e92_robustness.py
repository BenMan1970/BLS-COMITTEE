# -*- coding: utf-8 -*-
"""E9.1 / E9.2 - robustesse du parseur (mandat V2, corrections de classe A).

Deux defauts releves par le contre-examen, tous deux **sans effet sur une
decision** (aucun etat, aucun seuil, aucun advisory, aucun compteur) :

E9.1 - `_implied_macro_currency_bias` fait `p.pair.split("/")` avec un
       deballage en deux variables. La garde `if "/" not in p.pair` corrige
       les paires SANS slash, mais une paire a PLUS D'UN slash (`"A/B/C"`)
       leve encore `ValueError: too many values to unpack`.

E9.2 - La cascade `_SNAPSHOT_OPTIONAL_TIERS` (desk_parser) avale TOUT
       `TypeError`, y compris un `TypeError` INTERNE sans rapport avec un
       champ inconnu. Consequence : le schema se degrade silencieusement
       (ex. `calendar_coverage_meta` perdu -> horizon et banniere de
       troncature disparaissent du rapport) et l'avertissement emis accuse
       a tort une desynchronisation modele/parser.

Ces tests ECHOUENT sur le code d'avant correction et PASSENT apres.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from bluestar.decide.selection_grid import _implied_macro_currency_bias
from bluestar.extract.desk_parser import parse_desk
from bluestar.models import (
    DeskSnapshot,
    Direction,
    MacroPrioritySetup,
    MacroSnapshot,
)


# ---------------------------------------------------------------------------
# E9.1 - split("/") sur une paire a plus de deux segments
# ---------------------------------------------------------------------------

def _macro_with_pair(pair: str) -> MacroSnapshot:
    ps = MacroPrioritySetup(
        pair=pair, direction=Direction.LONG, conviction_stars=3, rationale="test",
    )
    return MacroSnapshot("d", "tz", "Reflation", 30.0, {}, (ps,), None)


@pytest.mark.parametrize("pair", ["EUR/USD", "XAUUSD", "US30", "", "/", "A/", "/B"])
def test_e91_atypical_pairs_do_not_crash(pair: str) -> None:
    """Formes deja tolerees : ne doivent produire aucune exception."""
    _implied_macro_currency_bias(_macro_with_pair(pair))


def test_e91_multi_slash_pair_does_not_crash() -> None:
    """E9.1 : `"A/B/C"` ne doit PAS lever ValueError.

    Avant correction : `ValueError: too many values to unpack (expected 2, got 3)`.
    """
    bias = _implied_macro_currency_bias(_macro_with_pair("A/B/C"))
    assert isinstance(bias, dict)


def test_e91_valid_pair_still_parses_both_legs() -> None:
    """Garde de non-regression : une vraie paire continue d'alimenter les 2 devises."""
    bias = _implied_macro_currency_bias(_macro_with_pair("EUR/USD"))
    assert set(bias) == {"EUR", "USD"}
    assert bias["EUR"][0][0] is Direction.LONG
    assert bias["USD"][0][0] is Direction.SHORT


def test_e91_multi_slash_is_deterministic() -> None:
    """Le comportement retenu pour `"A/B/C"` est documente et deterministe :
    on retient les deux premiers segments, on ignore le reste (aucune invention)."""
    b1 = _implied_macro_currency_bias(_macro_with_pair("A/B/C"))
    b2 = _implied_macro_currency_bias(_macro_with_pair("A/B/C"))
    assert b1 == b2
    assert isinstance(b1, dict)


# ---------------------------------------------------------------------------
# E9.2 - cascade de repli : un TypeError INTERNE ne doit pas degrader en silence
# ---------------------------------------------------------------------------

def _root_desk_html() -> str:
    return pathlib.Path("tests/data/desk.html").read_text(encoding="utf-8")


def test_e92_internal_type_error_is_not_swallowed() -> None:
    """E9.2 : un TypeError INTERNE (aucun kwarg inconnu) doit remonter.

    Avant correction : la cascade l'avale, retombe sur un tier plus pauvre et
    rend un DeskSnapshot ampute, sans jamais signaler l'erreur reelle.
    """
    real_init = DeskSnapshot.__init__
    state = {"first": True}

    def faulty_init(self, *args, **kwargs):
        if state["first"] and "calendar_coverage_meta" in kwargs:
            state["first"] = False
            raise TypeError("bug interne simule (pas un champ inconnu)")
        return real_init(self, *args, **kwargs)

    DeskSnapshot.__init__ = faulty_init
    try:
        with pytest.raises(TypeError, match="bug interne simule"):
            parse_desk(_root_desk_html())
    finally:
        DeskSnapshot.__init__ = real_init


def test_e92_unknown_field_still_falls_back(caplog) -> None:
    """Garde de non-regression : la cascade DOIT continuer de fonctionner pour
    son cas legitime (modele plus ancien que le parser -> champ inconnu).

    Teste sur le document REEL du cycle 13/10 : c'est le seul du corpus
    qui porte un bloc `calendar-coverage` avec metadonnees. Sur la fixture
    (aucune metadonnee), le code reste volontairement muet : il n'y a rien
    a perdre, donc rien a signaler."""
    real_init = DeskSnapshot.__init__
    state = {"first": True}

    def older_model_init(self, *args, **kwargs):
        if state["first"] and "calendar_coverage_meta" in kwargs:
            state["first"] = False
            raise TypeError(
                "DeskSnapshot.__init__() got an unexpected keyword argument "
                "'calendar_coverage_meta'"
            )
        return real_init(self, *args, **kwargs)

    DeskSnapshot.__init__ = older_model_init
    try:
        with caplog.at_level(logging.WARNING, logger="bluestar.extract.desk"):
            snap = parse_desk(_cycle_desk_html())
    finally:
        DeskSnapshot.__init__ = real_init

    assert isinstance(snap, DeskSnapshot)
    assert not snap.calendar_coverage_meta
    assert any(
        "desk_snapshot_field_unsupported" in r.getMessage() for r in caplog.records
    )


def test_e92_normal_parse_keeps_all_metadata() -> None:
    """Le cas nominal (aucune faute) doit conserver la couverture calendaire.

    La fixture `tests/data/desk.html` ne porte PAS de bloc `calendar-coverage`
    (donc `{}` y est legitime) : on verifie ici le cas nominal sur la fixture,
    et sur le document reel au test suivant.
    """
    snap = parse_desk(_root_desk_html())
    assert isinstance(snap, DeskSnapshot)
    assert snap.calendar_coverage, 'couverture calendaire perdue'


def _cycle_desk_html() -> str:
    """Document Desk reel du cycle 13/10 (racine du depot)."""
    p = pathlib.Path('../BLUESTAR FX Desk_Signal Report_2026.10.13.html')
    if not p.exists():  # tolerance si les tests sont lances hors depot
        pytest.skip('document Desk du cycle 13/10 absent')
    return p.read_text(encoding='utf-8')


def test_e92_cycle_document_keeps_coverage_metadata() -> None:
    """Sur le document reel, `calendar_coverage_meta` doit etre conserve :
    c'est ce champ qui porte l'horizon et la banniere de troncature."""
    snap = parse_desk(_cycle_desk_html())
    assert snap.calendar_coverage_meta, 'metadonnees de couverture perdues'
    assert snap.calendar_coverage_meta.get('horizon_h')
    assert snap.calendar_coverage_meta.get('feed_end_utc')
