"""
Grille de décision Bluestar v2 — fonctions pures, sans I/O.

Implémente la Partie 4.1 de la spécification institutionnelle :
- prédicat echo(leg) gelé, sur seuils numériques explicites (résout la critique
  du Head of Quant Research : "écho macro cohérent" n'était défini nulle part
  comme une fonction dans le prompt v1) ;
- état BLOCKED_DATA prioritaire sur tout scoring en cas de contradiction
  inter-sources (résout la critique du Risk Management) ;
- mode "jambe unique" explicite pour instruments non pairés, avec exclusion
  plutôt que scoring forcé (résout la critique de l'expert cross-asset) ;
- sortie en 5 états, jamais un score composite.

Toute constante de seuil est nommée et regroupée en tête de fichier
(Roadmap #6 : "registre gelé des seuils"). Modifier un seuil = modifier ce
fichier, jamais une interprétation au moment de la décision.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from enum import Enum

from bluestar.models import (
    DeskSetup,
    DeskSnapshot,
    Direction,
    IPSZone,
    MacroSnapshot,
)

logger = logging.getLogger("bluestar.decide")


# ---------------------------------------------------------------------------
# SEUILS GELÉS (registre versionné — Roadmap #6)
#
# Les seuils IPS (capitulation < 20, crowded > 80) NE SONT PLUS déclarés ici.
# Ils vivent exclusivement dans bluestar.models (IPS_CAPITULATION_MAX,
# IPS_CROWDED_MIN, consommés par ips_zone()) — c'est le seul endroit où la
# logique de zonage s'exécute réellement. Ce module y accède uniquement via
# CurrencyMacroData.zone (cf. echo_leg ci-dessous), jamais par une copie
# locale des valeurs numériques. C'est la correction du bug trouvé lors du
# round d'audit du 19/07/2026 (registre "gelé" dupliqué mais jamais lu).
#
# MAX_TECH_AGE_DAYS et MIN_RISK_REWARD : seuils propres à ce module, réellement
# utilisés ci-dessous. Statut de calibration honnête (relevé convergent des
# audits Claude 4.8 / GPT-5.5 / Kimi K2) : ce sont des valeurs choisies par
# l'auteur du système, PAS dérivées d'un backtest, d'une convention desk ou
# d'une décision de comité datée. "Gelé et versionné" signifie ici
# "traçable et non modifiable silencieusement", PAS "calibré et validé".
# ---------------------------------------------------------------------------
MAX_TECH_AGE_DAYS = 45          # NON CALIBRÉ — valeur provisoire de l'auteur, pas un empirique
MIN_RISK_REWARD = 1.5           # NON CALIBRÉ — valeur provisoire de l'auteur, pas un empirique
MAX_IPS_AGE_DAYS_WARN = 5       # RÉSERVÉ, NON CÂBLÉ — déclaré mais aucun code ne le consulte
                                 # encore (décote de fraîcheur IPS = roadmap, pas implémentée).
                                 # Volontairement non utilisé plutôt que silencieusement ignoré :
                                 # tout futur retrait ou activation doit passer par ce commentaire.
GRID_VERSION = "bluestar-decide-v2.1"  # incrémenté : correction seuils dupliqués (19/07/2026)


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
    BLOCKED_RISK = "BLOCKED_RISK"  # non calculé ici : nécessite le moteur de portefeuille (hors périmètre de decide())


@dataclass(frozen=True)
class LegEcho:
    currency: str
    verdict: LegVerdict
    detail: str


@dataclass(frozen=True)
class Decision:
    pair: str
    direction: Direction
    state: DecisionState
    legs: tuple[LegEcho, ...]
    limiting_factor: str            # jamais "score insuffisant" — cause nommée
    advisories: tuple[str, ...] = ()  # signaux non bloquants, jamais utilisés pour changer `state`
    grid_version: str = GRID_VERSION


def _implied_macro_currency_bias(macro: MacroSnapshot) -> dict[str, list[tuple[Direction, str, int]]]:
    """Pour chaque devise, liste les (direction implicite, paire d'origine, étoiles)
    déduites des setups prioritaires macro. Ex : GBP/USD SHORT (macro) implique
    une thèse macro baissière sur GBP (short) et haussière sur USD (long) —
    même si aucun setup desk ne porte sur GBP/USD lui-même."""
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
    """
    Signal NON bloquant : pour chaque jambe du setup, vérifie si la devise porte
    une thèse macro directionnelle implicite (déduite d'un AUTRE setup prioritaire
    macro que la paire exacte du desk) qui contredit le sens de cette jambe ici.

    C'est le mécanisme qui aurait signalé GBP/AUD long comme discutable même sans
    intervention manuelle : GBP porte une thèse macro SHORT via GBP/USD et GBP/JPY
    (aucun des deux n'est GBP/AUD), donc ce prédicat la détecte au niveau devise —
    sans jamais changer `state`. La décision d'escalader cet advisory en règle
    bloquante reste un choix humain explicite (cf. README, section limitations).
    """
    legs = setup.leg_currencies()
    if legs is None:
        return ()
    long_ccy, short_ccy = legs
    bias_map = _implied_macro_currency_bias(macro)
    advisories: list[str] = []

    for currency, leg_direction in ((long_ccy, Direction.LONG), (short_ccy, Direction.SHORT)):
        for implied_dir, origin_pair, stars in bias_map.get(currency, []):
            if origin_pair == setup.pair:
                continue  # déjà couvert par _macro_priority_conflict (conflit direct sur la paire exacte)
            if implied_dir != leg_direction:
                advisories.append(
                    f"devise {currency} : thèse macro implicite {implied_dir.value.upper()} "
                    f"via {origin_pair} ({stars}★), contredit la jambe {leg_direction.value} "
                    f"de ce setup — non bloquant, cf. limitations connues"
                )
    return tuple(advisories)


def echo_leg(currency_code: str, is_long_leg: bool, macro: MacroSnapshot) -> LegEcho:
    """
    Prédicat gelé évaluant une seule jambe (une devise) d'un setup.

    Règle explicite (remplace la définition circulaire du prompt v1) :
      - IPS indisponible                                   -> INDETERMINE
      - IPS en zone normale (20 <= IPS <= 80)               -> NEUTRE
      - IPS extrême (< 20)
          - jambe LONGUE  (on achète une devise en capitulation) -> CONFLUENCE (mean-reversion)
          - jambe COURTE  (on vend une devise déjà en capitulation, donc déjà
            "crowded short") -> CONFLIT (on enfonce un positionnement déjà extrême,
            risque de squeeze)
      - IPS extrême (> 80) -> symétrique : jambe courte = CONFLUENCE, jambe longue = CONFLIT
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


def _macro_priority_conflict(setup: DeskSetup, macro: MacroSnapshot) -> str | None:
    """Retourne un message si la paire est explicitement priorisée par le macro
    dans une direction opposée à celle du desk. None sinon."""
    for p in macro.priority_setups:
        if p.pair == setup.pair and p.direction != setup.direction:
            return (f"conflit macro frontal : le macro priorise {p.pair} "
                    f"{p.direction.value.upper()} ({p.conviction_stars}★) alors que "
                    f"le desk propose {setup.direction.value.upper()}")
    return None


def decide_setup(setup: DeskSetup, macro: MacroSnapshot) -> Decision:
    """Fonction pure : (DeskSetup, MacroSnapshot) -> Decision.
    Aucun effet de bord, aucun I/O — testable par golden files (cf. tests/)."""

    advisories = currency_level_advisories(setup, macro)

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
        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.REJECT,
            legs=(), limiting_factor=(
                "instrument non pairé (pas deux devises FX) — mode jambe unique "
                "non implémenté dans cette version, exclu explicitement plutôt que "
                "scoré au forceps avec une règle FX inadaptée"
            ),
            advisories=advisories,
        )

    long_ccy, short_ccy = legs
    leg_long = echo_leg(long_ccy, is_long_leg=True, macro=macro)
    leg_short = echo_leg(short_ccy, is_long_leg=False, macro=macro)
    leg_echoes = (leg_long, leg_short)

    # --- Niveau 3 : grille par jambe -----------------------------------------
    verdicts = {leg_long.verdict, leg_short.verdict}

    if LegVerdict.CONFLIT in verdicts:
        conflicting = leg_long if leg_long.verdict == LegVerdict.CONFLIT else leg_short
        return Decision(
            pair=setup.pair, direction=setup.direction, state=DecisionState.WATCH,
            legs=leg_echoes, limiting_factor=(
                f"conflit de positionnement sur la jambe {conflicting.currency} "
                f"({conflicting.detail})"
            ),
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

    # Aucune jambe en confluence, aucune en conflit -> tout au plus neutre/indéterminé
    return Decision(
        pair=setup.pair, direction=setup.direction, state=DecisionState.WATCH,
        legs=leg_echoes, limiting_factor="aucune confluence macro positive sur les deux jambes",
        advisories=advisories,
    )


def decide_all(desk: DeskSnapshot, macro: MacroSnapshot) -> tuple[Decision, ...]:
    """Applique decide_setup à tous les setups validés du desk."""
    decisions = tuple(decide_setup(s, macro) for s in desk.setups)
    counts = Counter(d.state.value for d in decisions)
    logger.info("decisions_computed grid_version=%s total=%d states=%s",
                GRID_VERSION, len(decisions), dict(counts))
    return decisions
