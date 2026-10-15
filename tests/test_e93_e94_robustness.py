# -*- coding: utf-8 -*-
"""E9.3 / E9.4 - deux defauts de robustesse supplementaires (classe A).

Tous deux de la MEME classe que E9.1/E9.2 : un document malforme fait planter
ou degrade silencieusement le cycle. Aucun etat, aucun seuil, aucun advisory,
aucun compteur n'est touche par les correctifs.

E9.3 - `technical_currency_advisories` (selection_grid, ligne ~365) :
       `sig_base, sig_quote = sig.symbol.split("/")` n'est garde que par
       `"/" not in sig.symbol`. Un symbole de correlation a 2+ slash
       (`"A/B/C"`) leve `ValueError: too many values to unpack` -> crash du
       cycle. Identique a E9.1, dans le chemin de decision.

E9.4 - cascade `_SETUP_OPTIONAL_TIERS` (desk_parser, ligne ~509) : meme
       defaut que E9.2 sur la cascade snapshot. Elle avale TOUT `TypeError`,
       y compris un bug interne a `DeskSetup`. Consequence mesuree :
       `take_profit_1` / `take_profit_2` disparaissent en silence (None) du
       setup retenu, et le log INFO accuse a tort une absence de champ dans
       `bluestar.models`.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from bluestar.decide.selection_grid import technical_currency_advisories
from bluestar.extract.desk_parser import parse_desk
from bluestar.models import (
    CorrelationSignal,
    DeskSetup,
    Direction,
    MacroSnapshot,
)


# ---------------------------------------------------------------------------
# E9.3 - symbole de correlation a plus de deux segments
# ---------------------------------------------------------------------------

def _corr(sym: str) -> CorrelationSignal:
    return CorrelationSignal(
        symbol=sym, direction=Direction.LONG, kind="corr",
        timeframe="D1", mtf_pct=50.0, quality="B", confluence=0.5,
    )


def _setup(pair: str = "EUR/USD") -> DeskSetup:
    return DeskSetup(
        pair=pair, direction=Direction.LONG, conviction_grade="B",
        conviction_value=0.5, cluster_tag="", quality="A", mtf_pct=50.0,
        age_days=10, risk_reward=2.0, factors={}, entry=1.0, stop_loss=0.9,
    )


@pytest.mark.parametrize("sym", ["EUR/USD", "XAUUSD", "/", "A/"])
def test_e93_atypical_correlation_symbols_do_not_crash(sym: str) -> None:
    technical_currency_advisories(_setup(), {"EUR": (_corr(sym),)})


def test_e93_multi_slash_correlation_symbol_does_not_crash() -> None:
    """E9.3 : `"A/B/C"` ne doit PAS lever ValueError.

    Avant correction : `ValueError: too many values to unpack (expected 2, got 3)`.
    """
    out = technical_currency_advisories(_setup(), {"EUR": (_corr("A/B/C"),)})
    assert isinstance(out, tuple)


def test_e93_valid_symbol_still_analysed() -> None:
    """Garde de non-regression : un symbole valide reste analyse.

    `EUR/USD` avec un signal LONG sur EUR produit une advisory de confluence.
    """
    out = technical_currency_advisories(_setup("EUR/USD"), {"EUR": (_corr("EUR/USD"),)})
    assert isinstance(out, tuple)
    # le symbole identique a la paire du setup est ignore (garde existante)
    assert out == ()


# ---------------------------------------------------------------------------
# E9.4 - cascade de repli du setup : TypeError INTERNE non avale
# ---------------------------------------------------------------------------

def _fixture_desk_html() -> str:
    return pathlib.Path("tests/data/desk.html").read_text(encoding="utf-8")


def test_e94_internal_type_error_is_not_swallowed() -> None:
    """E9.4 : un TypeError INTERNE a `DeskSetup` doit remonter.

    Avant correction : la cascade l'avale, retombe sur un tier plus pauvre et
    rend des setups AMPUTES (take_profit_1/2 perdus), en accusant a tort
    `bluestar.models` de ne pas declarer le champ.
    """
    real_init = DeskSetup.__init__
    state = {"first": True}

    def faulty_init(self, *args, **kwargs):
        if state["first"] and kwargs.get("take_profit_2") is not None:
            state["first"] = False
            raise TypeError("bug interne simule dans DeskSetup (pas un champ inconnu)")
        return real_init(self, *args, **kwargs)

    DeskSetup.__init__ = faulty_init
    try:
        with pytest.raises(TypeError, match="bug interne simule"):
            parse_desk(_fixture_desk_html())
    finally:
        DeskSetup.__init__ = real_init


def test_e94_unknown_field_still_falls_back(caplog) -> None:
    """Garde de non-regression : le repli legitime doit continuer de marcher."""
    real_init = DeskSetup.__init__
    state = {"first": True}

    def older_model_init(self, *args, **kwargs):
        if state["first"] and kwargs.get("take_profit_2") is not None:
            state["first"] = False
            raise TypeError(
                "DeskSetup.__init__() got an unexpected keyword argument "
                "'take_profit_2'"
            )
        return real_init(self, *args, **kwargs)

    DeskSetup.__init__ = older_model_init
    try:
        with caplog.at_level(logging.INFO, logger="bluestar.extract.desk"):
            snap = parse_desk(_fixture_desk_html())
    finally:
        DeskSetup.__init__ = real_init

    assert snap.setups, "les setups doivent rester extraits"
    assert any(
        "desk_setup_field_unsupported" in r.getMessage() for r in caplog.records
    ), "le repli doit rester SIGNALE"


def test_e94_normal_parse_keeps_take_profits() -> None:
    """Le cas nominal doit conserver take_profit_1 / take_profit_2 quand le
    document les porte et que le modele les accepte."""
    snap = parse_desk(_fixture_desk_html())
    assert snap.setups
    assert any(s.take_profit_1 is not None for s in snap.setups), "tp1 perdu"
    assert any(s.take_profit_2 is not None for s in snap.setups), "tp2 perdu"