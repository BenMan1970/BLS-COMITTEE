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
import types
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
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
GRID_VERSION = "bluestar-decide-v2.2"  # incremente : invariant 33/33 (rejets desk routes +
                                        # mode jambe unique indices/metaux) - round audit 27/07/2026


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


class AssetClass(str, Enum):
    """Classe d'actif, indépendante de la grille macro×IPS (qui ne
    s'applique qu'aux paires FX à deux devises). Ajoutée pour fermer
    l'invariant §1.1 ('33/33, jamais 3/33') : avant cette extension, tout
    instrument dont le symbole ne se décomposait pas en deux codes ISO-like
    de 3 lettres (indices actions, ex. SPX500/USD, US30/USD, NAS100/USD,
    DE30/EUR) était REJECT par construction, avec le même message générique
    — ce n'est pas un rejet motivé, c'est une catégorie non gérée."""
    FX_PAIR = "FX_PAIR"
    EQUITY_INDEX = "EQUITY_INDEX"
    METAL = "METAL"
    OTHER = "OTHER"


# Codes alpha-3 de métaux précieux : passent le même test syntaxique qu'une
# devise FX (3 lettres) mais n'en sont pas une — XAU/USD reste une paire à
# deux "devises" valides pour leg_currencies() (comportement existant,
# documenté, non modifié), mais on la classe correctement ici pour
# l'affichage et pour le mode jambe unique des futurs instruments similaires
# qui ne passeraient pas ce test (ex. un symbole sans second code à 3 lettres).
_METAL_CODES = frozenset({"XAU", "XAG", "XPT", "XPD"})

# Bases d'indices actions connues du corpus réel (round d'audit 27/07/2026).
# Liste non exhaustive : le test principal de classification reste "la base
# contient un chiffre" (SPX500, US30, NAS100, DE30...), ce qui couvre les
# nouveaux indices sans mise à jour de cette liste. Conservée pour les rares
# bases sans chiffre (ex. futurs "UK100", "DAX" selon la convention du desk).
_KNOWN_INDEX_BASES = frozenset({"SPX500", "US30", "NAS100", "DE30", "UK100", "JPN225"})


def classify_asset(pair: str) -> AssetClass:
    """Classe un symbole d'actif à partir de son code seul (pas de la
    direction). Ancrage : §3.5/§3.6 du cahier d'extension du comité — les
    indices actions du corpus réel ont un symbole dont la base contient un
    chiffre ; les métaux ont un code alpha-3 reconnu qui passe le même test
    syntaxique qu'une devise FX sans en être une."""
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


def regime_bias(asset_class: AssetClass, regime: str) -> Direction | None:
    """Biais directionnel implicite du régime macro pour un actif non-FX en
    mode jambe unique (§4 du cahier d'extension). Lecture délibérément
    grossière, par mots-clés Risk-On/Risk-Off dans le libellé de régime : un
    régime ambigu (ex. 'Mixed / Selective', corpus réel du 27/07/2026) ne
    produit AUCUN biais (None) plutôt qu'un biais forcé — cf. invariant
    'jamais de forcing' (§4)."""
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
        # Métal = valeur refuge : Risk-Off pousse vers le long, Risk-On vers
        # le short — symétrique de l'indice actions.
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


def technical_currency_advisories(
    setup: DeskSetup, correlation_groups: Mapping[str, tuple] = types.MappingProxyType({})
) -> tuple[str, ...]:
    """
    Signal NON bloquant, complémentaire de currency_level_advisories() : au lieu
    d'une thèse macro implicite déduite d'un setup prioritaire, compare chaque
    jambe du setup aux signaux TECHNIQUES réels (CHoCH etc.) déjà calculés par
    le moteur pour cette devise sur d'AUTRES paires (merged_pipeline.json ::
    correlation_groups, transporté depuis le round du 27/07/2026).

    Différence avec currency_level_advisories() : ici la contradiction vient
    d'un signal technique confirmé sur un autre instrument (ex. XAU/USD et
    NAS100/USD déjà baissiers en USD), pas d'une extrapolation macro. Ne
    change jamais `state` — même contrat que toutes les autres advisories.
    """
    legs = setup.leg_currencies()
    if legs is None or not correlation_groups:
        return ()
    long_ccy, short_ccy = legs
    advisories: list[str] = []

    for currency, leg_direction in ((long_ccy, Direction.LONG), (short_ccy, Direction.SHORT)):
        for sig in correlation_groups.get(currency, ()):
            if sig.symbol == setup.pair or sig.direction is None or "/" not in sig.symbol:
                continue  # même paire, signal Neutral, ou instrument non pairé (rien à inverser)
            sig_base, sig_quote = sig.symbol.split("/")
            if currency == sig_base:
                implied_dir = sig.direction
            elif currency == sig_quote:
                implied_dir = Direction.SHORT if sig.direction == Direction.LONG else Direction.LONG
            else:
                continue  # ne devrait pas arriver (clé du dict != devise du symbole), garde défensive
            if implied_dir != leg_direction:
                advisories.append(
                    f"devise {currency} : signal technique {sig.kind} confirme {currency} "
                    f"{implied_dir.value.upper()} via {sig.symbol} ({sig.timeframe}, qualité "
                    f"{sig.quality}), contredit la jambe {leg_direction.value} de ce setup — "
                    f"non bloquant, corrélation technique réelle (pas une extrapolation macro)"
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


def decide_setup(
    setup: DeskSetup, macro: MacroSnapshot,
    correlation_groups: Mapping[str, tuple] = types.MappingProxyType({}),
) -> Decision:
    """Fonction pure : (DeskSetup, MacroSnapshot) -> Decision.
    Aucun effet de bord, aucun I/O — testable par golden files (cf. tests/).

    `correlation_groups` est optionnel (PATCH-CORRGROUPS, round du 28/07/2026) :
    absent, la fonction se comporte exactement comme avant ce round."""

    advisories = currency_level_advisories(setup, macro) + technical_currency_advisories(
        setup, correlation_groups
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
            bias = regime_bias(asset_class, macro.regime)
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


_REJECT_CODE_ROUTES: dict[str, tuple[DecisionState, str]] = {
    "LOW_QUALITY": (DecisionState.REJECT, "quality"),
    "LOW_CONVICTION": (DecisionState.REJECT, "conviction"),
    "RR_OUT_OF_RANGE": (DecisionState.REJECT, "risk_reward"),
    "PRICE_PAST_TP": (DecisionState.BLOCKED_DATA, "price"),
    "CLUSTER_DUP": (DecisionState.WATCH, "cluster"),
    # --- Ajout round d'audit du 27/07/2026 (lecture directe de ENGINE.V9.py) ---
    # Ces 6 codes existent réellement dans le moteur (GateCode + preflight)
    # mais n'étaient routés nulle part avant ce correctif : ils tombaient dans
    # le repli générique (REJECT, "reject_code_inconnu"), un état arbitraire
    # pour des cas qui ne sont pas tous de même nature.
    "CAL_BLACKOUT": (DecisionState.BLOCKED_DATA, "calendar"),
    # Suspension temporaire liée au calendrier macro, pas un jugement
    # technique définitif — le moteur lui-même la traite à part de ses vrais
    # rejets (bloc "SUSPENDUS", distinct du tableau "REJETS" dans son propre
    # template). BLOCKED_DATA reflète ce même distinguo côté comité, plutôt
    # que le REJECT générique qui gommerait la différence.
    "SCHEMA_ASSET_ERROR": (DecisionState.BLOCKED_DATA, "integrity"),
    # Anomalie d'intégrité des données en amont (MTF manquant) — pas un
    # jugement sur le setup lui-même.
    "NO_ATR": (DecisionState.BLOCKED_DATA, "missing_data"),
    # Donnée technique requise absente (ATR ≤ 0) — même logique que
    # SCHEMA_ASSET_ERROR : donnée manquante, pas actif jugé et écarté.
    "NO_DIRECTION": (DecisionState.REJECT, "no_direction"),
    # Consensus MTF neutre : un vrai jugement technique ("pas de direction
    # exploitable"), donc REJECT au même titre que LOW_QUALITY.
    "LOW_CONSENSUS": (DecisionState.REJECT, "consensus"),
    # Même famille que LOW_QUALITY/LOW_CONVICTION : seuil technique non
    # atteint, jugement définitif pour ce cycle.
    "SL_SIGN": (DecisionState.BLOCKED_DATA, "computation_error"),
    # Anomalie de calcul interne (stop-loss du mauvais côté de l'entrée) —
    # signale un problème de génération du setup, pas un jugement de marché.
    # --- PATCH-CLUSTERDUP (round du 31/07/2026) ---
    # Nouveaux codes émis par v10.py après correction de diversify() (même
    # round). AVANT ce patch, exposition-devise, corrélation et dépassement
    # de MAX_SETUPS tombaient tous sous l'étiquette CLUSTER_DUP (faux :
    # aucun des trois n'est un doublon de cluster). Même état WATCH que
    # CLUSTER_DUP par cohérence : dans les quatre cas, ce n'est pas une
    # faute du setup lui-même, c'est une contrainte de construction de
    # portefeuille (exposition, corrélation, capacité), pas un jugement
    # technique définitif comme LOW_QUALITY/NO_DIRECTION.
    #
    # DÉPLOIEMENT ATOMIQUE OBLIGATOIRE avec le patch v10.py correspondant.
    # Vérifié par exécution (round du 31/07/2026) : si v10.py émet ces
    # codes sans que cette table soit mise à jour, decide_rejection()
    # route vers le repli (DecisionState.REJECT, "reject_code_inconnu") —
    # un setup simplement capé par une limite de portefeuille se
    # retrouverait promu REJECT dur, une régression de sévérité pire que
    # le bug d'origine (CLUSTER_DUP au moins routait déjà vers WATCH).
    "EXPOSURE_CAP": (DecisionState.WATCH, "exposure"),
    "CORRELATION_CAP": (DecisionState.WATCH, "correlation"),
    "MAX_SETUPS_REACHED": (DecisionState.WATCH, "capacity"),
}


def decide_rejection(rejected: DeskRejectedSetup, macro: MacroSnapshot) -> Decision:
    """Route un rejet desk vers une Decision.

    Ces actifs ont deja ete ecartes par le desk technique pour une raison
    purement technique (qualite, conviction, R:R, prix, doublon de
    cluster) et n'ont jamais traverse la grille macro x IPS echo(leg), qui
    ne s'applique qu'aux setups valides. decide_rejection() ne reevalue
    donc pas ces setups au niveau macro ; il trace une decision motivee
    pour chacun, ce qui ferme l'invariant 33/33 sans pretendre a une
    re-analyse qu'aucune donnee ne permettrait de justifier."""
    asset_class = classify_asset(rejected.pair)
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


def decide_all(
    desk: DeskSnapshot, macro: MacroSnapshot, *, include_rejects: bool = True
) -> tuple[Decision, ...]:
    """Applique decide_setup a tous les setups valides du desk et, par
    defaut, decide_rejection a tous les rejets desk — invariant souverain
    (len(desk.setups) + len(desk.rejected) == desk.universe_total decisions).

    include_rejects=False restaure l'ancien comportement (setups valides
    seuls) pour compatibilite explicite, opt-in — jamais le defaut
    silencieux."""
    decisions = [decide_setup(s, macro, desk.correlation_groups) for s in desk.setups]
    if include_rejects:
        decisions.extend(decide_rejection(r, macro) for r in desk.rejected)
    decisions_t = tuple(decisions)
    counts = Counter(d.state.value for d in decisions_t)
    logger.info("decisions_computed grid_version=%s total=%d states=%s include_rejects=%s",
                GRID_VERSION, len(decisions_t), dict(counts), include_rejects)
    return decisions_t
  
