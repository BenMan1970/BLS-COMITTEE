"""
Grille de décision Bluestar v2 — fonctions pures, sans I/O.

Implémente la Partie 4.1 de la spécification institutionnelle :
- prédicat echo(leg) gelé, sur seuils numériques explicites ;
- état BLOCKED_DATA prioritaire sur tout scoring en cas de contradiction
  inter-sources ;
- mode "jambe unique" explicite pour instruments non pairés ;
- sortie en 5 états, jamais un score composite.

Toute constante de seuil est nommée et regroupée en tête de fichier
(Roadmap #6 : "registre gelé des seuils"). Modifier un seuil = modifier ce
fichier, jamais une interprétation au moment de la décision.

ICF v2 (04/08/2026) — trois ajouts, TOUS additifs et non bloquants :
  - Proposition 1 : le motif de plafond de conviction du Desk (`cap_reason`)
    est injecté dans le `limiting_factor` affiché ;
  - Proposition 2 : advisory par jambe quand la devise est hors couverture
    calendaire déclarée par le Desk ;
  - Propositions 3 & 4 : deux fonctions PURES de diagnostic de synergie
    (`strength_theme_divergences`, `macro_priority_intersection_status`),
    consommées uniquement par la couche de rendu — elles ne retournent
    AUCUNE `Decision` et ne peuvent donc changer aucun état, par construction.
"""

from __future__ import annotations

import logging
import re
import types
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum

from bluestar.models import (
    DeskRejectedSetup,
    DeskSetup,
    DeskSnapshot,
    Direction,
    IPSZone,
    MacroSnapshot,
)

logger = logging.getLogger("bluestar.decide")


# ---------------------------------------------------------------------------
# X-9/G6 FIX (02/08/2026, gate G6).
#
# Aucune des trois couches ne vérifiait que le marché FX cash était réellement
# ouvert au moment de la génération, alors que le Desk peut publier une entrée
# "Market" même un week-end. Choix retenu : advisory non bloquante, cohérent
# avec le contrat de ce module (les advisories signalent, ne changent jamais
# l'état).
#
# Bornes UTC répliquées à l'identique de `macro_engine.session_label`
# (Fri 22:00 UTC -> Sun 22:00 UTC = marché fermé). Dette de duplication
# assumée : ce module n'importe pas macro_engine (application distincte).
# Toute modification des bornes là-bas DOIT être répercutée ici.
# ---------------------------------------------------------------------------
def _is_fx_market_closed(now_utc: datetime) -> bool:
    """True si le marché FX cash est fermé (week-end) à `now_utc` (UTC)."""
    wd = now_utc.weekday()  # 0 = lundi … 6 = dimanche
    if wd == 5:                            # samedi
        return True
    if wd == 6 and now_utc.hour < 22:      # dimanche avant réouverture
        return True
    if wd == 4 and now_utc.hour >= 22:     # vendredi après clôture
        return True
    return False


# ---------------------------------------------------------------------------
# SEUILS GELÉS (registre versionné — Roadmap #6)
#
# Les seuils IPS (capitulation < 20, crowded > 80) NE SONT PLUS déclarés ici.
# Ils vivent exclusivement dans bluestar.models (IPS_CAPITULATION_MAX,
# IPS_CROWDED_MIN, consommés par ips_zone()).
#
# Statut de calibration honnête : ce sont des valeurs choisies par l'auteur du
# système, PAS dérivées d'un backtest. "Gelé et versionné" signifie ici
# "traçable et non modifiable silencieusement", PAS "calibré et validé".
# ---------------------------------------------------------------------------
MAX_TECH_AGE_DAYS = 45          # NON CALIBRÉ — valeur provisoire de l'auteur
MIN_RISK_REWARD = 1.5           # NON CALIBRÉ — valeur provisoire de l'auteur
                                # NOTE F-09 : garde aujourd'hui INATTEIGNABLE (le
                                # preflight Desk rejette déjà tout R:R < 1.5). CONSERVÉE
                                # volontairement : une couche de validation indépendante
                                # doit re-vérifier les invariants de la couche précédente.
MAX_IPS_AGE_DAYS_WARN = 5       # RÉSERVÉ, NON CÂBLÉ — déclaré mais aucun code ne le lit
MAX_DESK_DOC_AGE_H = 3.0        # NON CALIBRÉ — garde de fraîcheur documentaire (audit B-2)
GRID_VERSION = "bluestar-decide-v2.5"
# v2.5 (04/08/2026, ICF v2 — audit de synergie inter-apps) : trois changements
# de sortie réels, tous additifs et non bloquants —
#   (P1) `cap_reason` du Desk injecté dans le `limiting_factor` ;
#   (P2) advisory de couverture calendaire par jambe ;
#   (P7) `factors_missing` transporté (plomberie, aucune règle ajoutée).
# Les diagnostics P3/P4 sont des fonctions pures hors du chemin `Decision` et
# ne justifieraient pas à eux seuls un bump — les trois précédents, si.
# Voir test_grid_version_is_pinned, qui existe précisément pour forcer cette
# revue à chaque changement.
# v2.4 (02/08/2026) : R-5, R-8, R-14, G6.

REGIME_BIAS_MIN_CONFIDENCE = 20.0
# R-8 FIX — NON CALIBRÉ par ce module ; valeur alignée sur le seuil de 20%
# observé dans le narratif macro produit en amont ("confiance 8% < seuil 20%
# -> régime reclassé"). Ce seuil vivait jusqu'ici exclusivement dans
# interpretation.py : le Comité n'avait AUCUNE garde indépendante et pouvait
# forcer un biais directionnel jambe-unique sur un régime peu fiable.

# ICF v2, Proposition 3 — seuils du diagnostic Force macro ↔ Thème desk.
# NON CALIBRÉS : bornes de lecture choisies pour ne déclencher que sur une
# divergence FRANCHE (une devise dans le tiers haut du classement de force
# alors que le Desk la déclare Bearish, ou l'inverse). La zone intermédiaire
# ]40 ; 60[ ne produit AUCUN diagnostic — deux mesures d'horizons différents
# ont le droit de ne pas se superposer sans que ce soit une anomalie.
STRENGTH_STRONG_MIN = 60.0
STRENGTH_WEAK_MAX = 40.0


# Libellés d'advisory associés au statut calendaire par setup (PATCH-CALSTATUS,
# audit F-05/C-04). BLACKOUT n'y figure pas volontairement : un setup en
# blackout ne parvient jamais jusqu'ici (le Desk le route en CAL_BLACKOUT, donc
# en decide_rejection, pas en decide_setup).
_CAL_STATUS_ADVISORY = {"PROXIMITY": "proximité calendaire", "WATCH": "surveillance calendaire"}

_TZ_OFFSET_RE = re.compile(r"^GMT([+-])(\d+)$")


def _desk_doc_datetime(desk: DeskSnapshot) -> datetime | None:
    """Recompose l'horodatage UTC du document desk depuis les champs parsés
    (PATCH-FRESHNESS, audit B-2). None si non vérifiable — jamais de devinette."""
    try:
        dt_naive = datetime.strptime(desk.report_datetime, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None
    tzs = (desk.report_timezone or "").upper()
    if tzs in ("UTC", "GMT"):
        tz = timezone.utc
    elif tzs == "CET":
        tz = timezone(timedelta(hours=1))
    elif tzs == "CEST":
        tz = timezone(timedelta(hours=2))
    else:
        m = _TZ_OFFSET_RE.match(tzs)
        if not m:
            return None
        sign = 1 if m.group(1) == "+" else -1
        tz = timezone(sign * timedelta(hours=int(m.group(2))))
    return dt_naive.replace(tzinfo=tz)


class LegVerdict(str, Enum):
    CONFLUENCE = "confluence"   # IPS extrême + le setup va dans le sens du dénouement
    CONFLIT = "conflit"         # IPS extrême + le setup renforce la position extrême
    NEUTRE = "neutre"           # IPS en zone normale : le technique fait foi seul
    INDETERMINE = "indetermine" # donnée manquante (ex. IPS USD)


class DecisionState(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    WATCH = "WATCH"
    REJECT = "REJECT"
    BLOCKED_DATA = "BLOCKED_DATA"
    BLOCKED_RISK = "BLOCKED_RISK"  # non calculé ici : nécessite le moteur de portefeuille


class AssetClass(str, Enum):
    """Classe d'actif, indépendante de la grille macro×IPS (qui ne s'applique
    qu'aux paires FX à deux devises). Ferme l'invariant §1.1 ('33/33, jamais
    3/33') : avant cette extension, tout instrument dont le symbole ne se
    décomposait pas en deux codes ISO-like de 3 lettres était REJECT par
    construction, avec un message générique — ce n'est pas un rejet motivé,
    c'est une catégorie non gérée."""
    FX_PAIR = "FX_PAIR"
    EQUITY_INDEX = "EQUITY_INDEX"
    METAL = "METAL"
    OTHER = "OTHER"


_METAL_CODES = frozenset({"XAU", "XAG", "XPT", "XPD"})
_KNOWN_INDEX_BASES = frozenset({"SPX500", "US30", "NAS100", "DE30", "UK100", "JPN225"})


def classify_asset(pair: str) -> AssetClass:
    """Classe un symbole d'actif à partir de son code seul (pas de la
    direction). Ancrage §3.5/§3.6 : les indices actions du corpus réel ont un
    symbole dont la base contient un chiffre ; les métaux ont un code alpha-3
    reconnu qui passe le même test syntaxique qu'une devise FX sans en être une."""
    if "/" not in pair:
        return AssetClass.OTHER
    base, _, quote = pair.partition("/")
    if base in _METAL_CODES:
        return AssetClass.METAL
    if any(ch.isdigit() for ch in base) or base in _KNOWN_INDEX_BASES:
        return AssetClass.EQUITY_INDEX
    if len(base) == 3 and base.isalpha() and len(quote) == 3 and quote.isalpha():
        return AssetClass.FX_PAIR
    return AssetClass.OTHER


def regime_bias(
    asset_class: AssetClass, regime: str,
    confidence: float | None = None,
    min_confidence: float = REGIME_BIAS_MIN_CONFIDENCE,
) -> Direction | None:
    """Biais directionnel implicite du régime macro pour un actif non-FX en
    mode jambe unique (§4). Lecture délibérément grossière, par mots-clés
    Risk-On/Risk-Off : un régime ambigu (ex. 'Mixed / Selective') ne produit
    AUCUN biais (None) plutôt qu'un biais forcé — invariant 'jamais de forcing'.

    R-8 FIX : `confidence` est OPTIONNEL (défaut None) — tout appelant existant
    qui ne le fournit pas obtient un comportement strictement inchangé. Sous
    `min_confidence`, AUCUN biais n'est forcé même si le libellé contient
    'risk-on'/'risk-off'."""
    if confidence is not None and confidence < min_confidence:
        return None
    regime_lc = (regime or "").lower()
    is_risk_on = "risk-on" in regime_lc or "risk on" in regime_lc
    is_risk_off = "risk-off" in regime_lc or "risk off" in regime_lc
    if asset_class == AssetClass.EQUITY_INDEX:
        if is_risk_on:
            return Direction.LONG
        if is_risk_off:
            return Direction.SHORT
        return None
    if asset_class == AssetClass.METAL:
        # Métal = valeur refuge : Risk-Off pousse vers le long, Risk-On vers le
        # short — symétrique de l'indice actions.
        if is_risk_off:
            return Direction.LONG
        if is_risk_on:
            return Direction.SHORT
        return None
    return None


@dataclass(frozen=True)
class LegEcho:
    currency: str
    verdict: LegVerdict
    detail: str


@dataclass(frozen=True)
class Decision:
    pair: str
    direction: Direction | None
    state: DecisionState
    legs: tuple[LegEcho, ...]
    limiting_factor: str
    advisories: tuple[str, ...] = ()
    grid_version: str = GRID_VERSION
    asset_class: AssetClass = AssetClass.FX_PAIR
    source_reject_code: str | None = None


@dataclass(frozen=True)
class StrengthThemeDivergence:
    """ICF v2, Proposition 3 — constat de divergence entre DEUX mesures
    différentes de la « force » d'une devise, produites par deux applications
    distinctes sur deux horizons distincts. N'est PAS une `Decision` :
    ce type ne peut, par construction, modifier aucun état."""
    currency: str
    macro_strength_score: float
    macro_strength_rank: int | None
    desk_theme: str
    detail: str


def _implied_macro_currency_bias(macro: MacroSnapshot) -> dict[str, list[tuple[Direction, str, int]]]:
    """Pour chaque devise, liste les (direction implicite, paire d'origine, étoiles)
    déduites des setups prioritaires macro. Ex : GBP/USD SHORT (macro) implique
    une thèse macro baissière sur GBP et haussière sur USD — même si aucun setup
    desk ne porte sur GBP/USD lui-même."""
    bias: dict[str, list[tuple[Direction, str, int]]] = {}
    for p in macro.priority_setups:
        if "/" not in p.pair:
            continue
        base, quote = p.pair.split("/")
        base_dir = p.direction
        quote_dir = Direction.SHORT if base_dir == Direction.LONG else Direction.LONG
        bias.setdefault(base, []).append((base_dir, p.pair, p.conviction_stars))
        bias.setdefault(quote, []).append((quote_dir, p.pair, p.conviction_stars))
    return bias


def currency_level_advisories(setup: DeskSetup, macro: MacroSnapshot) -> tuple[str, ...]:
    """Signal NON bloquant : pour chaque jambe du setup, vérifie si la devise
    porte une thèse macro directionnelle implicite (déduite d'un AUTRE setup
    prioritaire macro que la paire exacte du desk) qui contredit le sens de
    cette jambe ici. Ne change jamais `state` — l'escalade en règle bloquante
    reste un choix humain explicite."""
    legs = setup.leg_currencies()
    if legs is None:
        return ()
    long_ccy, short_ccy = legs
    bias_map = _implied_macro_currency_bias(macro)
    advisories: list[str] = []

    for currency, leg_direction in ((long_ccy, Direction.LONG), (short_ccy, Direction.SHORT)):
        for implied_dir, origin_pair, stars in bias_map.get(currency, []):
            if origin_pair == setup.pair:
                continue  # déjà couvert par _macro_priority_conflict
            if implied_dir != leg_direction:
                advisories.append(
                    f"devise {currency} : thèse macro implicite {implied_dir.value.upper()} "
                    f"via {origin_pair} ({stars}★), contredit la jambe {leg_direction.value} "
                    f"de ce setup — non bloquant, cf. limitations connues"
                )
    return tuple(advisories)


def technical_currency_advisories(
    setup: DeskSetup, correlation_groups: Mapping[str, tuple] = types.MappingProxyType({})
) -> tuple[str, ...]:
    """Signal NON bloquant, complémentaire de currency_level_advisories() : au
    lieu d'une thèse macro implicite, compare chaque jambe du setup aux signaux
    TECHNIQUES réels (CHoCH etc.) déjà calculés par le moteur pour cette devise
    sur d'AUTRES paires. Ne change jamais `state`."""
    legs = setup.leg_currencies()
    if legs is None or not correlation_groups:
        return ()
    long_ccy, short_ccy = legs
    advisories: list[str] = []

    for currency, leg_direction in ((long_ccy, Direction.LONG), (short_ccy, Direction.SHORT)):
        for sig in correlation_groups.get(currency, ()):
            if sig.symbol == setup.pair or sig.direction is None or "/" not in sig.symbol:
                continue  # même paire, signal Neutral, ou instrument non pairé
            sig_base, sig_quote = sig.symbol.split("/")
            if currency == sig_base:
                implied_dir = sig.direction
            elif currency == sig_quote:
                implied_dir = Direction.SHORT if sig.direction == Direction.LONG else Direction.LONG
            else:
                continue  # garde défensive
            if implied_dir != leg_direction:
                advisories.append(
                    f"devise {currency} : signal technique {sig.kind} confirme {currency} "
                    f"{implied_dir.value.upper()} via {sig.symbol} ({sig.timeframe}, qualité "
                    f"{sig.quality}), contredit la jambe {leg_direction.value} de ce setup — "
                    f"non bloquant, corrélation technique réelle (pas une extrapolation macro)"
                )
    return tuple(advisories)


def calendar_coverage_advisories(pair: str, uncovered_currencies: frozenset[str]) -> tuple[str, ...]:
    """ICF v2, Proposition 2 — advisory NON bloquante par jambe.

    Le Desk déclare, au niveau DOCUMENT, quelles devises son flux calendrier
    couvre réellement (filtre producteur). Une jambe portant sur une devise
    hors couverture affiche pourtant un `cal_status = OK` vert : « OK » y
    signifie « non mesuré », pas « dégagé ». Cette advisory joint enfin le
    constat document-niveau à la ligne de décision concernée.

    Silencieuse (tuple vide) si le Desk n'a pas déclaré sa couverture — une
    couverture non déclarée n'autorise AUCUNE conclusion, ni dans un sens ni
    dans l'autre, et surtout pas une advisory sur toutes les lignes."""
    if not uncovered_currencies or "/" not in pair:
        return ()
    out: list[str] = []
    for token in pair.split("/"):
        code = token.strip().upper()
        if len(code) == 3 and code.isalpha() and code in uncovered_currencies:
            out.append(
                f"couverture calendaire : devise {code} absente du flux calendrier du Desk "
                f"— un statut calendaire « OK » sur cette jambe signifie « non mesuré », "
                f"pas « dégagé » ; non bloquant, à arbitrer avant toute action sur ce setup"
            )
    return tuple(out)


def echo_leg(currency_code: str, is_long_leg: bool, macro: MacroSnapshot) -> LegEcho:
    """
    Prédicat gelé évaluant une seule jambe (une devise) d'un setup.

    Règle explicite :
      - IPS indisponible                          -> INDETERMINE
      - IPS en zone normale (20 <= IPS <= 80)      -> NEUTRE
      - IPS extrême (< 20)
          - jambe LONGUE  -> CONFLUENCE (mean-reversion)
          - jambe COURTE  -> CONFLIT (on enfonce un positionnement déjà extrême)
      - IPS extrême (> 80) -> symétrique
    """
    data = macro.currencies.get(currency_code)
    if data is None or data.ips is None:
        return LegEcho(currency_code, LegVerdict.INDETERMINE,
                        "IPS non disponible dans les documents fournis")

    zone = data.zone
    if zone == IPSZone.NORMAL:
        return LegEcho(currency_code, LegVerdict.NEUTRE,
                        f"IPS {data.ips:.0f} en zone normale — le technique fait foi seul sur cette jambe")

    if zone == IPSZone.CAPITULATION:
        if is_long_leg:
            return LegEcho(currency_code, LegVerdict.CONFLUENCE,
                            f"IPS {data.ips:.0f} (capitulation) ; jambe longue = dénouement mean-reversion")
        return LegEcho(currency_code, LegVerdict.CONFLIT,
                        f"IPS {data.ips:.0f} (capitulation) ; jambe courte renforce un short déjà crowded — risque de squeeze")

    if zone == IPSZone.CROWDED:
        if is_long_leg:
            return LegEcho(currency_code, LegVerdict.CONFLIT,
                            f"IPS {data.ips:.0f} (crowded) ; jambe longue renforce un long déjà crowded — risque de squeeze")
        return LegEcho(currency_code, LegVerdict.CONFLUENCE,
                        f"IPS {data.ips:.0f} (crowded) ; jambe courte = dénouement mean-reversion")

    return LegEcho(currency_code, LegVerdict.INDETERMINE, "zone IPS non résolue")


# R-14 FIX (02/08/2026) : le Desk nomme les indices avec une notation "paire"
# (DE30/EUR, US30/USD) ; le Macro les nomme par leur symbole nu (DAX, US30).
# `_macro_priority_conflict` comparait par égalité stricte : une priorité macro
# sur "DAX" ne pouvait structurellement jamais être confrontée à un setup Desk
# sur "DE30/EUR". Correspondances reprises telles que citées par l'audit.
_INDEX_PAIR_ALIASES: dict[str, str] = {
    "DE30/EUR": "DAX",
    "US30/USD": "US30",
    "NAS100/USD": "NAS100",
    "SPX500/USD": "SPX500",
}


def _normalize_pair(pair: str) -> str:
    """Normalise une notation Desk (paire) vers la notation Macro (symbole nu)
    quand un alias d'indice existe ; retourne `pair` inchangé sinon."""
    return _INDEX_PAIR_ALIASES.get(pair, pair)


def _macro_priority_conflict(setup: DeskSetup, macro: MacroSnapshot) -> str | None:
    """Retourne un message si la paire est explicitement priorisée par le macro
    dans une direction opposée à celle du desk. None sinon."""
    setup_pair_norm = _normalize_pair(setup.pair)
    for p in macro.priority_setups:
        if _normalize_pair(p.pair) == setup_pair_norm and p.direction != setup.direction:
            return (f"conflit macro frontal : le macro priorise {p.pair} "
                    f"{p.direction.value.upper()} ({p.conviction_stars}★) alors que "
                    f"le desk propose {setup.direction.value.upper()}")
    return None


def _decide_setup_core(
    setup: DeskSetup, macro: MacroSnapshot,
    correlation_groups: Mapping[str, tuple] = types.MappingProxyType({}),
    now: datetime | None = None,
) -> Decision:
    """Fonction pure : (DeskSetup, MacroSnapshot) -> Decision.
    Aucun effet de bord, aucun I/O — testable par golden files.

    PATCH-F6BIS : ce corps est un renommage pur de l'ancien `decide_setup` —
    AUCUNE ligne de logique de décision n'y a été modifiée depuis. Les
    enrichissements (flags, cal_status, cap_reason, couverture calendaire)
    vivent dans le wrapper `decide_setup`, ce qui garantit qu'un setup nu
    produit un `Decision` strictement identique à avant ces patchs."""

    advisories = currency_level_advisories(setup, macro) + technical_currency_advisories(
        setup, correlation_groups
    )

    # X-9/G6 FIX : qualification explicite d'une entrée Market générée marché
    # fermé — advisory non bloquante, jamais un changement d'état.
    entry_type = getattr(setup, "entry_type", None)
    if now is not None and entry_type == "Market" and _is_fx_market_closed(now):
        advisories = advisories + (
            f"entrée Market générée marché FX déclaré fermé (week-end, {now:%Y-%m-%d %H:%M} UTC) "
            f"— le prix utilisé comme niveau d'entrée n'est pas un prix de marché actif ; "
            f"non bloquant ici, à arbitrer avant toute action sur ce setup",
        )

    # --- Niveau 1 : intégrité / invalidation ---------------------------------
    if setup.age_days is not None and setup.age_days > MAX_TECH_AGE_DAYS:
        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.BLOCKED_DATA,
            legs=(), limiting_factor=(
                f"setup âgé de {setup.age_days}j, au-delà du plafond dur "
                f"({MAX_TECH_AGE_DAYS}j) — signal considéré périmé"
            ),
            advisories=advisories,
        )

    priority_conflict = _macro_priority_conflict(setup, macro)
    if priority_conflict is not None:
        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.BLOCKED_DATA,
            legs=(), limiting_factor=priority_conflict, advisories=advisories,
        )

    # --- Niveau 2 : régime d'instrument (pairé vs jambe unique) -------------
    legs = setup.leg_currencies()
    if legs is None:
        asset_class = classify_asset(setup.pair)

        if asset_class in (AssetClass.EQUITY_INDEX, AssetClass.METAL):
            bias = regime_bias(asset_class, macro.regime, confidence=macro.regime_confidence_pct)
            if bias is None:
                return Decision(
                    pair=setup.pair, direction=setup.direction, state=DecisionState.WATCH,
                    legs=(), asset_class=asset_class, limiting_factor=(
                        f"mode jambe unique ({asset_class.value}) : regime macro "
                        f"{macro.regime!r} non directionnel"
                    ),
                    advisories=advisories,
                )
            if setup.direction == bias:
                return Decision(
                    pair=setup.pair, direction=setup.direction, state=DecisionState.ELIGIBLE,
                    legs=(), asset_class=asset_class, limiting_factor="—",
                    advisories=advisories + (
                        f"mode jambe unique : confluence avec le biais de regime "
                        f"{bias.value.upper()} ({asset_class.value}, regime {macro.regime!r})",
                    ),
                )
            return Decision(
                pair=setup.pair, direction=setup.direction, state=DecisionState.WATCH,
                legs=(), asset_class=asset_class, limiting_factor=(
                    f"mode jambe unique : conflit avec le biais de regime "
                    f"{bias.value.upper()} ({asset_class.value}, regime {macro.regime!r})"
                ),
                advisories=advisories,
            )

        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.REJECT,
            legs=(), asset_class=asset_class, limiting_factor=(
                "instrument non paire et non classifiable (ni FX, ni indice actions, "
                "ni metal reconnu)"
            ),
            advisories=advisories,
        )

    long_ccy, short_ccy = legs
    leg_long = echo_leg(long_ccy, is_long_leg=True, macro=macro)
    leg_short = echo_leg(short_ccy, is_long_leg=False, macro=macro)
    leg_echoes = (leg_long, leg_short)

    # --- Niveau 3 : grille par jambe -----------------------------------------
    verdicts = {leg_long.verdict, leg_short.verdict}

    conflicting_legs = [leg for leg in leg_echoes if leg.verdict == LegVerdict.CONFLIT]
    if conflicting_legs:
        # PATCH-C05 (audit C-05/F-12) : l'ancienne version ne retenait que le
        # PREMIER conflit — un setup dont les deux jambes sont en capitulation
        # n'en citait qu'une, masquant que le squeeze pouvait se déclencher des
        # deux côtés. Le cas mono-conflit produit une chaîne strictement
        # identique à avant ce patch.
        if len(conflicting_legs) == 1:
            limiting = (f"conflit de positionnement sur la jambe "
                        f"{conflicting_legs[0].currency} ({conflicting_legs[0].detail})")
        else:
            limiting = ("conflit de positionnement sur les DEUX jambes — "
                        + " ; ".join(f"jambe {c.currency} ({c.detail})" for c in conflicting_legs)
                        + " — squeeze possible dans les deux sens")
        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.WATCH,
            legs=leg_echoes, limiting_factor=limiting,
            advisories=advisories,
        )

    # --- Niveau 4 : qualité technique minimale --------------------------------
    if setup.risk_reward is not None and setup.risk_reward < MIN_RISK_REWARD:
        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.REJECT,
            legs=leg_echoes, limiting_factor=f"R:R {setup.risk_reward} sous le plancher {MIN_RISK_REWARD}",
            advisories=advisories,
        )

    # --- Éligibilité ------------------------------------------------------
    if LegVerdict.CONFLUENCE in verdicts:
        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.ELIGIBLE,
            legs=leg_echoes, limiting_factor="—", advisories=advisories,
        )

    return Decision(
        pair=setup.pair, direction=setup.direction, state=DecisionState.WATCH,
        legs=leg_echoes, limiting_factor="aucune confluence macro positive sur les deux jambes",
        advisories=advisories,
    )


def _augment_limiting_factor_with_flags(decision: Decision, setup: DeskSetup) -> Decision:
    """PATCH-F6BIS : fait apparaître les flags Desk (C1-C10) dans le
    `limiting_factor` affiché au Comité, pour que celui-ci reflète la vérité de
    la couche technique.

    Garanties de non-régression :
    - `getattr(setup, "flags", ())` : si `DeskSetup` ne porte pas ce champ, on
      obtient `()` et la fonction retourne `decision` INCHANGÉE ;
    - un setup sans flags ressort avec un `Decision` strictement identique ;
    - seul le TEXTE du `limiting_factor` change, jamais le `state`.
    """
    flags = getattr(setup, "flags", ())
    if not flags:
        return decision

    def _get(flag: object, key: str) -> str | None:
        if isinstance(flag, dict):
            return flag.get(key)
        return getattr(flag, key, None)

    major_parts: list[str] = []
    minor_parts: list[str] = []
    for f in flags:
        sev = _get(f, "severity")
        code = _get(f, "code")
        detail = _get(f, "detail")
        if not code:
            continue
        txt = f"{code} · {detail}" if detail else code
        if sev == "major":
            major_parts.append(txt)
        elif sev == "minor":
            # V4-07 : afficher aussi les flags mineurs pour visibilité.
            minor_parts.append(txt)

    flag_text = "; ".join(major_parts + minor_parts)
    if not flag_text:
        return decision

    clean_lf = decision.limiting_factor
    if "[flags desk majeurs :" in clean_lf:
        match = re.search(r"\[flags desk majeurs : ([^\]]+)\]", clean_lf)
        if match:
            existing_flags = match.group(1)
            if existing_flags not in flag_text:
                flag_text = existing_flags + "; " + flag_text
            clean_lf = clean_lf.replace(match.group(0), "")
    elif "[flags desk" in clean_lf:
        clean_lf = re.sub(r"\[flags desk [^\]]+\]", "", clean_lf)

    return replace(decision, limiting_factor=clean_lf + f" [flags desk : {flag_text}]")


def _augment_limiting_factor_with_cap_reason(decision: Decision, setup: DeskSetup) -> Decision:
    """ICF v2, Proposition 1 — surface le motif de plafond de conviction posé
    par la couche technique (`<div class="cap-note">` côté Desk).

    Constat qui motive ce patch : sur le cycle audité, les setups publiés sont
    TOUS plafonnés par le Desk (« risque macro NON ÉVALUÉ », « risque
    structurel critique (REVERSAL_RISK) ») et le Comité affichait pourtant un
    facteur limitant qui n'en disait rien.

    Garanties :
    - `getattr(setup, "cap_reason", None)` : champ absent ou setup non
      plafonné -> `decision` retournée INCHANGÉE ;
    - modifie UNIQUEMENT le texte du `limiting_factor`, jamais le `state` ;
    - idempotent (garde de sous-chaîne) ;
    - AUCUN double comptage : le plafond est déjà reflété dans le grade de
      conviction produit par le Desk ; on divulgue le MOTIF, on ne repénalise
      rien."""
    cap_reason = getattr(setup, "cap_reason", None)
    if not cap_reason:
        return decision
    if cap_reason in decision.limiting_factor:
        return decision
    return replace(
        decision,
        limiting_factor=f"{decision.limiting_factor} [cap desk : {cap_reason}]",
    )


def decide_setup(
    setup: DeskSetup, macro: MacroSnapshot,
    correlation_groups: Mapping[str, tuple] = types.MappingProxyType({}),
    now: datetime | None = None,
    uncovered_currencies: frozenset[str] = frozenset(),
) -> Decision:
    """Point d'entrée public. Signature élargie de deux paramètres OPTIONNELS
    (`now`, `uncovered_currencies`) : tout appelant existant obtient un
    comportement inchangé.

    Voir `_decide_setup_core` pour la logique de décision elle-même, et les
    trois `_augment_*` / advisories ci-dessous pour les enrichissements
    d'affichage, dont aucun ne modifie `state`."""
    decision = _decide_setup_core(setup, macro, correlation_groups, now=now)
    decision = _augment_limiting_factor_with_flags(decision, setup)
    decision = _augment_limiting_factor_with_cap_reason(decision, setup)

    # PATCH-CALSTATUS (audit F-05/C-04) : statut calendaire par setup, surfacé
    # en advisory NON bloquant. `getattr` défensif.
    cal_status = getattr(setup, "cal_status", None)
    if cal_status in _CAL_STATUS_ADVISORY:
        cal_note = getattr(setup, "cal_note", "") or ""
        decision = replace(
            decision,
            advisories=decision.advisories + (
                f"statut calendaire Desk : {cal_status} — {_CAL_STATUS_ADVISORY[cal_status]}"
                + (f" ({cal_note})" if cal_note else "")
                + " — non bloquant ici, à arbitrer avant toute action sur ce setup",
            ),
        )

    # ICF v2, Proposition 2 : couverture calendaire par jambe.
    cov = calendar_coverage_advisories(setup.pair, uncovered_currencies)
    if cov:
        decision = replace(decision, advisories=decision.advisories + cov)

    return decision


_REJECT_CODE_ROUTES: dict[str, tuple[DecisionState, str]] = {
    "LOW_QUALITY": (DecisionState.REJECT, "quality"),
    "LOW_CONVICTION": (DecisionState.REJECT, "conviction"),
    "RR_OUT_OF_RANGE": (DecisionState.REJECT, "risk_reward"),
    "PRICE_PAST_TP": (DecisionState.BLOCKED_DATA, "price"),
    "CLUSTER_DUP": (DecisionState.WATCH, "cluster"),
    # --- Ajout round du 27/07/2026 (lecture directe de ENGINE.V9.py) ---
    "CAL_BLACKOUT": (DecisionState.BLOCKED_DATA, "calendar"),
    # Suspension temporaire liée au calendrier macro, pas un jugement technique
    # définitif — le moteur lui-même la traite à part de ses vrais rejets.
    "SCHEMA_ASSET_ERROR": (DecisionState.BLOCKED_DATA, "integrity"),
    "NO_ATR": (DecisionState.BLOCKED_DATA, "missing_data"),
    "NO_DIRECTION": (DecisionState.REJECT, "no_direction"),
    "LOW_CONSENSUS": (DecisionState.REJECT, "consensus"),
    "SL_SIGN": (DecisionState.BLOCKED_DATA, "computation_error"),
    # --- PATCH-CLUSTERDUP (31/07/2026) ---
    # Contraintes de construction de portefeuille (exposition, corrélation,
    # capacité), pas un jugement technique définitif -> WATCH, comme CLUSTER_DUP.
    # DÉPLOIEMENT ATOMIQUE OBLIGATOIRE avec le patch v10.py correspondant.
    "EXPOSURE_CAP": (DecisionState.WATCH, "exposure"),
    "CORRELATION_CAP": (DecisionState.WATCH, "correlation"),
    "MAX_SETUPS_REACHED": (DecisionState.WATCH, "capacity"),
}


def _macro_priority_conflict_for_rejected(
    rejected: DeskRejectedSetup, macro: MacroSnapshot
) -> str | None:
    """Retourne un message si la paire/direction est priorisée par le macro dans
    un sens opposé. Utilisé par decide_rejection pour signaler les conflits
    macro non bloquants (garde-fou B-1).

    `DeskRejectedSetup.direction` est typé `Direction | None` — un rejet peut
    n'avoir AUCUNE direction connue (ex. CAL_BLACKOUT détecté avant toute
    analyse directionnelle). On ne peut pas déclarer un « conflit frontal »
    sans connaître la direction du desk : silencieux (None) dans ce cas."""
    if rejected.direction is None:
        return None
    rejected_pair_norm = _normalize_pair(rejected.pair)
    for p in macro.priority_setups:
        if _normalize_pair(p.pair) == rejected_pair_norm and p.direction != rejected.direction:
            return (f"conflit macro frontal : le macro priorise {p.pair} "
                    f"{p.direction.value.upper()} ({p.conviction_stars}★) alors que "
                    f"le desk propose {rejected.direction.value.upper()} — non bloquant.")
    return None


def _reject_currency_ips_advisories(
    rejected: DeskRejectedSetup, macro: MacroSnapshot
) -> tuple[str, ...]:
    """R-5 FIX (02/08/2026) : l'alerte de positionnement extrême (IPS)
    n'atteignait jamais le Comité quand TOUS les setups portant une jambe sur
    la devise concernée étaient rejetés par le Desk avant la grille macro×IPS.

    Advisory NON bloquante, symétrique de B-1 : ne change JAMAIS l'état du
    rejet. Silencieuse pour tout instrument non pairé et pour toute zone IPS
    non extrême. Silencieuse aussi quand `direction is None` : sans direction,
    juger CONFLIT/CONFLUENCE reviendrait à inventer une prémisse."""
    if rejected.direction is None:
        return ()
    if "/" not in rejected.pair:
        return ()
    base_ccy, quote_ccy = rejected.pair.split("/")
    advisories: list[str] = []
    for currency, is_long_leg in (
        (base_ccy, rejected.direction == Direction.LONG),
        (quote_ccy, rejected.direction == Direction.SHORT),
    ):
        leg = echo_leg(currency, is_long_leg, macro)
        if leg.verdict in (LegVerdict.CONFLIT, LegVerdict.CONFLUENCE):
            advisories.append(
                f"positionnement devise {currency} : {leg.detail} — setup rejeté pour raison "
                f"technique par ailleurs, information macro conservée pour la lecture "
                f"d'ensemble du Comité ; non bloquant."
            )
    return tuple(advisories)


def decide_rejection(rejected: DeskRejectedSetup, macro: MacroSnapshot) -> Decision:
    """Route un rejet desk vers une Decision.

    Ces actifs ont déjà été écartés par le desk technique pour une raison
    purement technique et n'ont jamais traversé la grille macro × IPS
    echo(leg), qui ne s'applique qu'aux setups valides. decide_rejection() ne
    réévalue donc pas ces setups au niveau macro ; il trace une décision
    motivée pour chacun, ce qui ferme l'invariant 33/33."""
    asset_class = classify_asset(rejected.pair)

    # B-1 : conflit de priorité macro (advisory non bloquant).
    macro_conflict = _macro_priority_conflict_for_rejected(rejected, macro)
    # R-5 : advisory de positionnement IPS extrême, symétrique de B-1.
    ips_advisories = _reject_currency_ips_advisories(rejected, macro)

    state, _leg_key = _REJECT_CODE_ROUTES.get(
        rejected.reject_code, (DecisionState.REJECT, "reject_code_inconnu")
    )
    if rejected.reject_code not in _REJECT_CODE_ROUTES:
        logger.warning("unknown_reject_code pair=%s code=%s", rejected.pair, rejected.reject_code)

    limiting_factor = f"rejet desk [{rejected.reject_code}] : {rejected.detail}"

    advisories: tuple[str, ...] = ()
    if rejected.reject_code == "CLUSTER_DUP":
        advisories = (
            "cluster deja represente par un setup valide du desk — ce "
            "doublon n'est pas promu automatiquement en ELIGIBLE.",
        )

    if macro_conflict:
        advisories = advisories + (macro_conflict,)
    advisories = advisories + ips_advisories

    return Decision(
        pair=rejected.pair,
        direction=rejected.direction,
        state=state,
        legs=(),
        limiting_factor=limiting_factor,
        advisories=advisories,
        asset_class=asset_class,
        source_reject_code=rejected.reject_code,
    )


# ---------------------------------------------------------------------------
# DIAGNOSTICS DE SYNERGIE (ICF v2, Propositions 3 & 4)
#
# Fonctions PURES, hors du chemin `Decision`. Elles ne retournent aucune
# `Decision`, ne sont appelées par aucune des fonctions `decide_*`, et sont
# consommées uniquement par la couche de rendu / le logger. Par construction,
# elles NE PEUVENT PAS modifier un état, un score ou un gate — c'est
# l'exigence explicite de l'audit (Rejet interne : « injecter le Currency
# Strength Ranking dans echo_leg ou dans un score » a été rejeté pour double
# comptage avec F2/F6 côté Desk).
# ---------------------------------------------------------------------------

_THEME_RE = re.compile(r"^([A-Za-z]{3})\s+(Bullish|Bearish)$", re.IGNORECASE)


def strength_theme_divergences(
    desk: DeskSnapshot, macro: MacroSnapshot
) -> tuple[StrengthThemeDivergence, ...]:
    """ICF v2, Proposition 3 — Règle Absolue 4.

    Deux applications mesurent la « force » d'une devise sous un vocabulaire
    proche mais avec deux constructions différentes, sur deux horizons
    différents :
      - Macro : `Currency Strength Ranking`, momentum de PRIX D1 (Oanda) ;
      - Desk  : `Thèmes`, biais STRUCTUREL issu du consensus multi-timeframe.

    Les deux sont extraites par le Comité (coût de parsing déjà payé, contrat
    de rupture dure côté Macro : 8 devises exigées) et ne se rencontraient
    jamais. Cette fonction les JOINT et NOMME la divergence — elle ne l'arbitre
    pas : une divergence n'est pas nécessairement une erreur, elle est souvent
    le signe légitime d'un désaccord d'horizon (momentum court contre structure
    longue), et c'est précisément pour cela qu'elle doit être exposée plutôt
    qu'écrasée par un des deux camps.

    Ne se déclenche que sur une divergence FRANCHE (cf. STRENGTH_STRONG_MIN /
    STRENGTH_WEAK_MAX) ; la zone intermédiaire ne produit rien. Silencieuse si
    les thèmes ou les scores de force sont indisponibles."""
    out: list[StrengthThemeDivergence] = []
    for raw in getattr(desk, "themes", ()) or ():
        m = _THEME_RE.match(str(raw).strip())
        if not m:
            continue
        ccy = m.group(1).upper()
        theme = m.group(2).capitalize()
        data = macro.currencies.get(ccy)
        if data is None or data.strength_score is None:
            continue
        score = float(data.strength_score)
        rank = data.strength_rank

        if theme == "Bullish" and score <= STRENGTH_WEAK_MAX:
            sense = "faible"
        elif theme == "Bearish" and score >= STRENGTH_STRONG_MIN:
            sense = "forte"
        else:
            continue

        rank_txt = f", rang {rank}/8" if rank is not None else ""
        out.append(StrengthThemeDivergence(
            currency=ccy,
            macro_strength_score=score,
            macro_strength_rank=rank,
            desk_theme=theme,
            detail=(
                f"{ccy} — momentum prix D1 [Macro/Oanda] : {score:.0f}/100 ({sense}{rank_txt}) "
                f"vs biais structurel MTF [Desk] : {theme}. Deux mesures DIFFÉRENTES sur deux "
                f"horizons différents, pas deux avis sur la même chose : une divergence n'est "
                f"pas nécessairement une erreur. Signalée, jamais arbitrée — aucun état, aucun "
                f"score, aucun gate n'en dépend."
            ),
        ))
    return tuple(out)


def macro_priority_intersection_status(
    desk: DeskSnapshot, macro: MacroSnapshot
) -> str | None:
    """ICF v2, Proposition 4 — étend le garde-fou B-4.

    B-4 couvre le cas « canal macro vide » (aucune thèse directionnelle). Il ne
    couvre PAS le cas « canal macro non vide mais DISJOINT » : des priorités
    macro existent, mais aucune ne porte sur un setup validé par le desk. Ce
    second cas est strictement plus trompeur, puisque le rapport affiche
    « Macro : ACTIF » et « aucun conflit frontal détecté » — alors qu'aucun
    conflit frontal n'était POSSIBLE par construction.

    Retourne None (silencieux) si le canal est vide (déjà couvert par B-4) ou
    si l'intersection est non vide (le garde-fou de conflit a réellement pu
    s'exercer)."""
    if not macro.priority_setups:
        return None  # cas déjà couvert et déclaré par B-4
    desk_pairs = {_normalize_pair(s.pair) for s in desk.setups}
    macro_pairs = {_normalize_pair(p.pair) for p in macro.priority_setups}
    if desk_pairs & macro_pairs:
        return None
    macro_lbl = ", ".join(
        f"{p.pair} {p.direction.value.upper()} ({p.conviction_stars}★)"
        for p in macro.priority_setups
    ) or "—"
    desk_lbl = ", ".join(sorted(s.pair for s in desk.setups)) or "aucun"
    return (
        f"canal macro ACTIF mais DISJOINT du desk : priorités macro [{macro_lbl}] ; "
        f"setups validés par le desk [{desk_lbl}] — intersection VIDE. Aucun conflit "
        f"frontal macro×desk n'était possible par construction sur ce cycle : "
        f"« aucun conflit détecté sur les setups validés » ne signifie donc PAS "
        f"« accord macro×desk »."
    )


def decide_all(
    desk: DeskSnapshot, macro: MacroSnapshot, *, include_rejects: bool = True,
    now: datetime | None = None,
) -> tuple[Decision, ...]:
    """Applique decide_setup à tous les setups valides du desk et, par défaut,
    decide_rejection à tous les rejets desk — invariant souverain
    (len(desk.setups) + len(desk.rejected) == desk.universe_total décisions).

    include_rejects=False restaure l'ancien comportement (setups valides seuls)
    pour compatibilité explicite, opt-in — jamais le défaut silencieux.

    now=None (défaut) préserve la pureté de la fonction pour les golden files :
    la garde de fraîcheur documentaire (audit B-2) ne s'active que si
    l'appelant fournit explicitement l'horloge (cf. cli.py)."""
    # PATCH-B4 (audit F-03/B-4) : quand le canal directionnel macro est vide,
    # _macro_priority_conflict et currency_level_advisories sont structurellement
    # inertes — « Advisories : aucun » signifie alors « aucune thèse macro
    # n'existait », pas « aucun conflit macro ».
    if not macro.priority_setups:
        logger.warning(
            "macro_priority_setups_empty — canal directionnel macro INERTE ce cycle : "
            "garde _macro_priority_conflict et advisories currency-level desactives. "
            "Le rapport final doit le declarer (audit B-4)."
        )
    else:
        # ICF v2, Proposition 4 : le cas « actif mais disjoint », non couvert par B-4.
        _inter = macro_priority_intersection_status(desk, macro)
        if _inter:
            logger.warning("macro_priority_intersection_empty — %s", _inter)

    uncovered = frozenset(getattr(desk, "calendar_coverage", {}).get("uncovered", frozenset()))
    if uncovered:
        logger.info(
            "calendar_coverage_uncovered currencies=%s — advisory par jambe active",
            sorted(uncovered),
        )

    decisions = [
        decide_setup(s, macro, desk.correlation_groups, now=now, uncovered_currencies=uncovered)
        for s in desk.setups
    ]

    # PATCH-FRESHNESS (audit B-2/F-04) : un document desk périmé ne doit plus
    # pouvoir produire des décisions setups sans marquage. Opt-in via now=.
    if now is not None:
        desk_dt = _desk_doc_datetime(desk)
        if desk_dt is None:
            logger.error(
                "desk_document_datetime_unverifiable — fraicheur NON demontrable "
                "(report_datetime=%r tz=%r)", desk.report_datetime, desk.report_timezone)
        else:
            age_h = (now - desk_dt).total_seconds() / 3600.0
            if age_h > MAX_DESK_DOC_AGE_H:
                logger.error(
                    "desk_document_stale age_h=%.2f seuil=%.1f — %d setup(s) retrogrades BLOCKED_DATA",
                    age_h, MAX_DESK_DOC_AGE_H, len(decisions))
                decisions = [
                    replace(
                        d, state=DecisionState.BLOCKED_DATA,
                        limiting_factor=(
                            f"document desk perime (age {age_h:.1f}h > seuil "
                            f"{MAX_DESK_DOC_AGE_H:.1f}h) — etat technique gele : "
                            f"{d.limiting_factor}"),
                    )
                    for d in decisions
                ]

    if include_rejects:
        rej = [decide_rejection(r, macro) for r in desk.rejected]
        # PATCH-CLUSTERDUP-XREF (audit C-12) : un doublon dédupliqué par le Desk
        # réapparaissait en WATCH au même niveau visuel que son représentant.
        # Advisory de contre-référence, sans changement d'état.
        rep_states = {d.pair: d.state.value for d in decisions}
        for i, r in enumerate(desk.rejected):
            if r.reject_code != "CLUSTER_DUP":
                continue
            m = re.search(r"=\s*([A-Za-z0-9]+/[A-Za-z0-9]+)", r.detail or "")
            if m and m.group(1) in rep_states:
                rej[i] = replace(rej[i], advisories=rej[i].advisories + (
                    f"representant {m.group(1)} present dans ce rapport en etat "
                    f"{rep_states[m.group(1)]} — doublon maintenu deduplique, "
                    f"ne pas le reintroduire manuellement au meme niveau de priorite",
                ))
        decisions.extend(rej)

    decisions_t = tuple(decisions)
    counts = Counter(d.state.value for d in decisions_t)
    logger.info("decisions_computed grid_version=%s total=%d states=%s include_rejects=%s",
                GRID_VERSION, len(decisions_t), dict(counts), include_rejects)
    return decisions_t
