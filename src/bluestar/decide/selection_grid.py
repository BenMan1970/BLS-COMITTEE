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
# X-9/G6 FIX (round de validation zero-régression, 02/08/2026, gate G6).
#
# Aucune des trois couches (Macro, Desk, Comité) ne vérifiait jusqu'ici que
# le marché FX cash était réellement ouvert au moment de la génération, alors
# que le Desk peut publier une entrée "Market" (qui utilise le prix courant
# tel quel comme niveau d'entrée) même un week-end. L'audit indépendant
# (rapport RUN-4, gate G6) exige a minima "une qualification explicite d'un
# ELIGIBLE Market sur marché déclaré fermé en amont" — c'est le choix retenu
# ici (advisory non bloquante), cohérent avec le contrat déjà établi dans ce
# module : les advisories signalent, elles ne changent jamais l'état.
#
# Bornes UTC répliquées à l'identique de `macro_engine.session_label`
# (Fri 22:00 UTC -> Sun 22:00 UTC = marché fermé). Dette de duplication
# assumée par choix : ce module (bluestar.decide) n'importe pas macro_engine
# (application Streamlit distincte, hors du package bluestar), au même titre
# que TIER_WINDOWS est dupliqué entre calendar_layer.py et v10.py plutôt que
# de créer un couplage inter-applications. Toute modification des bornes
# dans macro_engine.session_label DOIT être répercutée ici à l'identique.
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
                                # NOTE F-09 (audit 31/07/2026) : cette garde est aujourd'hui
                                # INATTEIGNABLE car le preflight Desk rejette déjà tout R:R < 1.5
                                # (RR_OUT_OF_RANGE) en amont. Elle est CONSERVÉE volontairement :
                                # une couche de validation indépendante doit re-vérifier les
                                # invariants de la couche précédente — elle redevient active
                                # automatiquement si le seuil Desk est un jour abaissé.
MAX_IPS_AGE_DAYS_WARN = 5       # RÉSERVÉ, NON CÂBLÉ — déclaré mais aucun code ne le consulte
                                 # encore (décote de fraîcheur IPS = roadmap, pas implémentée).
                                 # Volontairement non utilisé plutôt que silencieusement ignoré :
                                 # tout futur retrait ou activation doit passer par ce commentaire.
MAX_DESK_DOC_AGE_H = 3.0        # NON CALIBRÉ — garde de fraîcheur documentaire (audit B-2,
                                # round 31/07/2026). Au-delà, les setups (pas les rejets, qui
                                # restent des faits historiques) sont rétrogradés BLOCKED_DATA.
                                # Cas motivant : rapport du 31/07 publiant un WATCH sur un
                                # snapshot desk âgé de 3h37, 8 minutes avant un tier A.
GRID_VERSION = "bluestar-decide-v2.3"  # incremente : canal flags/cal_status, garde de fraicheur,
                                        # double conflit de jambes, contre-reference CLUSTER_DUP
                                        # - round de validation independante du 31/07/2026
REGIME_BIAS_MIN_CONFIDENCE = 20.0       # R-8 FIX (round du 02/08/2026) — NON CALIBRÉ par ce
                                        # module ; valeur alignée sur le seuil de 20% observé
                                        # dans le narratif macro produit en amont ("confiance
                                        # 8% < seuil 20% -> régime reclassé"). Ce seuil vivait
                                        # jusqu'ici exclusivement dans interpretation.py (absent
                                        # de ce corpus) : le Comité n'avait AUCUNE garde
                                        # indépendante et pouvait forcer un biais directionnel
                                        # jambe-unique sur un régime affiché mais peu fiable.
                                        # À recalibrer si le seuil amont change (re-test
                                        # obligatoire, cf. audit R-8).


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


def regime_bias(
    asset_class: AssetClass, regime: str,
    confidence: float | None = None,
    min_confidence: float = REGIME_BIAS_MIN_CONFIDENCE,
) -> Direction | None:
    """Biais directionnel implicite du régime macro pour un actif non-FX en
    mode jambe unique (§4 du cahier d'extension). Lecture délibérément
    grossière, par mots-clés Risk-On/Risk-Off dans le libellé de régime : un
    régime ambigu (ex. 'Mixed / Selective', corpus réel du 27/07/2026) ne
    produit AUCUN biais (None) plutôt qu'un biais forcé — cf. invariant
    'jamais de forcing' (§4).

    R-8 FIX (round de validation zero-régression, 02/08/2026) : `confidence`
    est OPTIONNEL et vaut `None` par défaut — tout appelant existant qui ne
    le fournit pas obtient un comportement strictement inchangé. Quand
    l'appelant fournit la confiance de régime publiée (ex.
    `macro.regime_confidence_pct`) et qu'elle est sous `min_confidence`,
    AUCUN biais n'est forcé, même si le libellé du régime contient
    'risk-on'/'risk-off' — un régime affiché à confiance publiée faible ne
    doit pas piloter un instrument jambe unique. Avant ce correctif, le
    Comité n'avait aucune garde indépendante sur ce point (audit R-8) : la
    seule protection connue vivait dans `interpretation.py`, absent de ce
    corpus."""
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


# R-14 FIX (round de validation zero-régression, 02/08/2026, MOYEN).
#
# Le Desk nomme les indices avec une notation "paire" (DE30/EUR, US30/USD,
# NAS100/USD, SPX500/USD) ; le Macro (et `config.INDICES`) les nomme par
# leur symbole nu (DAX, US30, NAS100, SPX500). `_macro_priority_conflict`
# compare `p.pair == setup.pair` par égalité stricte : une priorité macro
# publiée sur "DAX" ne peut structurellement jamais être confrontée à un
# setup Desk sur "DE30/EUR", quelle que soit la direction de l'un ou de
# l'autre (audit indépendant, rapport RUN-4, R-14 : "Une priorité macro sur
# indice ne peut structurellement jamais être confrontée au Desk"). Dormant
# à ce jour (aucune priorité macro sur indice observée), mais activable dès
# que le Macro publie une thèse directionnelle sur un indice.
#
# Correspondances reprises telles que citées par l'audit lui-même (pas
# déduites ni devinées) : DAX <-> DE30/EUR, US30 <-> US30/USD,
# NAS100 <-> NAS100/USD, SPX500 <-> SPX500/USD.
_INDEX_PAIR_ALIASES: dict[str, str] = {
    "DE30/EUR": "DAX",
    "US30/USD": "US30",
    "NAS100/USD": "NAS100",
    "SPX500/USD": "SPX500",
}


def _normalize_pair(pair: str) -> str:
    """Normalise une notation Desk (paire) vers la notation Macro (symbole
    nu) quand un alias d'indice existe ; retourne `pair` inchangé sinon
    (comportement identique à avant ce correctif pour tout instrument FX,
    XAU/USD, ou toute paire déjà dans la même notation des deux côtés)."""
    return _INDEX_PAIR_ALIASES.get(pair, pair)


def _macro_priority_conflict(setup: DeskSetup, macro: MacroSnapshot) -> str | None:
    """Retourne un message si la paire est explicitement priorisée par le macro
    dans une direction opposée à celle du desk. None sinon.

    R-14 FIX : comparaison normalisée (`_normalize_pair`) pour que les
    indices (notation Desk "DE30/EUR" vs notation Macro "DAX") puissent
    enfin se confronter. Zéro régression pour tout instrument sans alias :
    `_normalize_pair` retourne la chaîne inchangée."""
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
    Aucun effet de bord, aucun I/O — testable par golden files (cf. tests/).

    `correlation_groups` est optionnel (PATCH-CORRGROUPS, round du 28/07/2026) :
    absent, la fonction se comporte exactement comme avant ce round.

    `now` est optionnel (X-9/G6 FIX, round du 02/08/2026) : absent (défaut),
    aucune advisory de marché fermé n'est produite — comportement
    strictement inchangé pour tout appelant/golden file existant qui ne le
    fournit pas.

    PATCH-F6BIS (round du 31/07/2026) : ce corps de fonction est un
    renommage pur de l'ancien `decide_setup` — AUCUNE ligne de logique
    n'a été modifiée ici. Le nouveau `decide_setup` (plus bas) est un
    wrapper fin qui appelle cette fonction telle quelle puis fait passer
    son résultat dans `_augment_limiting_factor_with_flags`. Ce découpage
    garantit une non-régression totale : tout golden file / test existant
    qui appelait `decide_setup` sur un setup SANS flags majeurs obtient un
    `Decision` strictement identique à avant ce patch."""

    advisories = currency_level_advisories(setup, macro) + technical_currency_advisories(
        setup, correlation_groups
    )

    # X-9/G6 FIX : qualification explicite d'une entrée Market générée
    # marché fermé — advisory non bloquante, jamais un changement d'état.
    # `getattr` défensif : tant que bluestar.models.DeskSetup ne porte pas
    # encore `entry_type` (cf. patch desk_parser en 3 paliers), retourne
    # None et cette advisory ne se déclenche jamais — comportement
    # strictement inchangé dans ce cas.
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
        # PATCH-C05 (round du 31/07/2026, audit C-05/F-12) : l'ancienne version
        # ne retenait que le PREMIER CONFLIT — un setup dont les deux jambes sont
        # en capitulation (ex. EUR 10 / CAD 8) n'en citait qu'une, masquant au
        # lecteur final que le squeeze pouvait se déclencher des deux côtés
        # (symétrie avec _build_setup_positioning côté macro, qui gère déjà le
        # cas "DEUX jambes extrêmes"). Le cas mono-conflit produit une chaîne
        # strictement identique à avant ce patch.
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

    # Aucune jambe en confluence, aucune en conflit -> tout au plus neutre/indéterminé
    return Decision(
        pair=setup.pair, direction=setup.direction, state=DecisionState.WATCH,
        legs=leg_echoes, limiting_factor="aucune confluence macro positive sur les deux jambes",
        advisories=advisories,
    )


def _augment_limiting_factor_with_flags(decision: Decision, setup: DeskSetup) -> Decision:
    """PATCH-F6BIS (audit round 2, GLM-5.2, 31/07/2026) : fait apparaître les
    flags Desk de sévérité "major" (C1-C10, cf. `_extract_flags` côté
    parser) dans le `limiting_factor` affiché au Comité, pour que celui-ci
    reflète la vérité de la couche technique (cf. audit §10 : "la fonction
    decide_setup doit inspecter setup.flags [...] écraser ou compléter le
    limiting_factor").

    Garanties de non-régression :
    - `getattr(setup, "flags", ())` : si `DeskSetup` ne porte pas encore ce
      champ (schéma exact non démontrable depuis ce corpus — cf. commentaire
      dans `comite_final_desk_parser._parse_setup`), on obtient `()` et la
      fonction retourne `decision` INCHANGÉE. Zéro comportement nouveau tant
      que le champ n'existe pas réellement sur l'objet.
    - Un setup sans flags, ou avec uniquement des flags "minor", ressort
      avec un `Decision` strictement identique à avant ce patch (même
      `limiting_factor`, même `state`) — seule l'apparition d'au moins un
      flag "major" change la sortie, et uniquement le texte du
      `limiting_factor` (jamais le `state`, qui reste sous la seule autorité
      de `_decide_setup_core`, conformément à la demande de l'audit de
      COMPLÉTER l'affichage plutôt que de changer la décision).
    - Idempotent : si le texte du/des flag(s) majeur(s) figure déjà dans le
      `limiting_factor` (ex: si une future version de `_decide_setup_core`
      venait à les citer elle-même), rien n'est dupliqué.
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
            # V4-07 : aussi afficher les flags mineurs pour visibilité
            minor_parts.append(txt)

    # Réserver la priorité aux majors, mais ajouter les mineurs si présents
    flag_text = "; ".join(major_parts + minor_parts)
    if not flag_text:
        return decision

    # Nettoyer le texte existant de la décision
    clean_lf = decision.limiting_factor
    if "[flags desk majeurs :" in clean_lf:
        # Récupérer les flags existants pour les réintégrer
        import re
        match = re.search(r'\[flags desk majeurs : ([^\]]+)\]', clean_lf)
        if match:
            existing_flags = match.group(1)
            if existing_flags not in flag_text:
                flag_text = existing_flags + "; " + flag_text
            clean_lf = clean_lf.replace(match.group(0), "")
    elif "[flags desk" in clean_lf:
        clean_lf = re.sub(r'\[flags desk [^\]]+\]', '', clean_lf)

    return replace(
        decision,
        limiting_factor=clean_lf + f" [flags desk : {flag_text}]",
    )


def decide_setup(
    setup: DeskSetup, macro: MacroSnapshot,
    correlation_groups: Mapping[str, tuple] = types.MappingProxyType({}),
    now: datetime | None = None,
) -> Decision:
    """Point d'entrée public, comportement inchangé pour tout appelant
    existant (même signature élargie d'un seul paramètre optionnel `now`,
    défaut None -- cf. `_decide_setup_core` pour X-9/G6) -- voir
    `_decide_setup_core` pour la logique de décision elle-même et
    `_augment_limiting_factor_with_flags` pour le correctif F6-BIS (câblage
    des flags Desk C1-C10 majeurs dans le limiting_factor affiché au
    Comité)."""
    decision = _decide_setup_core(setup, macro, correlation_groups, now=now)
    decision = _augment_limiting_factor_with_flags(decision, setup)
    # PATCH-CALSTATUS (round du 31/07/2026, audit F-05/C-04) : le statut
    # calendaire par setup (OK/PROXIMITY/WATCH), rendu par le Desk dans
    # `.cal-row` mais jamais extrait avant le patch parser associé, est
    # surfacé ici en advisory NON bloquant. `getattr` défensif : tant que
    # DeskSetup ne porte pas le champ (schéma avant patch models.py B-1),
    # la sortie est strictement identique à avant.
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
    return decision


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


def _macro_priority_conflict_for_rejected(rejected: DeskRejectedSetup, macro: MacroSnapshot) -> Optional[str]:
    """Retourne un message si la paire/direction est priorisée par le macro dans un sens opposé.
    
    Utilisé par decide_rejection pour signaler les conflits macro non bloquants.

    R-14 FIX (round de validation zero-régression, 02/08/2026, MOYEN) :
    comparaison normalisée (`_normalize_pair`), symétrique du correctif déjà
    appliqué à `_macro_priority_conflict` ci-dessus — même raison (notation
    d'indice Desk "DE30/EUR" vs notation Macro "DAX"), même correction,
    pour que le garde-fou B-1 (conflit macro sur rejets) ne soit pas
    lui-même aveugle aux indices alors que le correctif vient de fermer ce
    trou côté setups valides.

    CORRECTIF SUPPLÉMENTAIRE (02/08/2026, après confirmation du schéma réel
    de bluestar/models.py) : `DeskRejectedSetup.direction` est typé
    `Direction | None` — un rejet peut n'avoir AUCUNE direction connue (ex.
    `CAL_BLACKOUT` détecté avant toute analyse directionnelle). Sans garde,
    `rejected.direction.value.upper()` plus bas aurait levé `AttributeError`
    sur `None`, faisant échouer `decide_rejection` pour CE rejet précis —
    un défaut réel, pas une simple dégradation. On ne peut de toute façon
    pas déclarer un "conflit frontal" sans connaître la direction du desk :
    silencieux (None) dans ce cas."""
    if rejected.direction is None:
        return None
    rejected_pair_norm = _normalize_pair(rejected.pair)
    for p in macro.priority_setups:
        if _normalize_pair(p.pair) == rejected_pair_norm and p.direction != rejected.direction:
            return (f"conflit macro frontal : le macro priorise {p.pair} "
                    f"{p.direction.value.upper()} ({p.conviction_stars}★) alors que "
                    f"le desk propose {rejected.direction.value.upper()} — non bloquant.")
    return None


def _reject_currency_ips_advisories(rejected: "DeskRejectedSetup", macro: MacroSnapshot) -> tuple[str, ...]:
    """R-5 FIX (round de validation zero-régression, 02/08/2026, CRITIQUE).

    L'alerte de positionnement extrême (IPS) n'atteignait jamais le Comité
    quand TOUS les setups portant une jambe sur la devise concernée étaient
    rejetés par le Desk avant la grille macro×IPS — exactement le cas
    confirmé par l'audit indépendant (rapport RUN-4, R-5) : USD en IPS 87
    (crowded) ce cycle, mais chaque setup à jambe USD est soit
    `CAL_BLACKOUT` soit rejeté techniquement, et `decide_rejection`
    n'appelait jusqu'ici jamais `echo_leg` (cette fonction ne s'exécute que
    pour les setups valides, dans `_decide_setup_core`).

    Advisory NON bloquante, symétrique du mécanisme B-1 (conflit de
    priorité macro sur les rejets) déjà en place ci-dessous : ne change
    JAMAIS l'état du rejet ; rend seulement visible une zone IPS extrême
    sur une devise que le Desk a écartée pour une tout autre raison.
    Silencieux (tuple vide) pour tout instrument non pairé (pas de "/" dans
    `rejected.pair`, ex. indices/commodités) et pour toute zone IPS non
    extrême (NEUTRE/INDETERMINE).

    CORRECTIF SUPPLÉMENTAIRE (02/08/2026, après confirmation du schéma réel
    de bluestar/models.py) : `rejected.direction` peut être `None`. Sans
    garde, les deux comparaisons `== Direction.LONG` / `== Direction.SHORT`
    deviennent silencieusement `False` pour les DEUX devises, ce qui
    revient à fabriquer une hypothèse de jambe ("aucune des deux n'est
    longue") sans aucune base réelle — pas un crash, mais un jugement
    CONFLIT/CONFLUENCE construit sur une prémisse inventée. Silencieux
    (tuple vide) est le choix honnête ici, cohérent avec le traitement déjà
    appliqué aux instruments non pairés juste au-dessus."""
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

    Ces actifs ont deja ete ecartes par le desk technique pour une raison
    purement technique (qualite, conviction, R:R, prix, doublon de
    cluster) et n'ont jamais traverse la grille macro x IPS echo(leg), qui
    ne s'applique qu'aux setups valides. decide_rejection() ne reevalue
    donc pas ces setups au niveau macro ; il trace une decision motivee
    pour chacun, ce qui ferme l'invariant 33/33.
    
    IMPORTANTE : B-1 — les conflits de priorité macro sur les rejets
    (direction macro ≠ direction desk) sont signalés en advisory NON
    bloquant — le rejet reste valide, mais le pistolage macro est visible."""
    asset_class = classify_asset(rejected.pair)
    
    # B-1 : vérifier le conflit de priorité macro (advisory non bloquant)
    macro_conflict = _macro_priority_conflict_for_rejected(rejected, macro)

    # R-5 FIX : advisory de positionnement IPS extrême, symétrique de B-1
    ips_advisories = _reject_currency_ips_advisories(rejected, macro)
    
    state, _leg_key = _REJECT_CODE_ROUTES.get(
        rejected.reject_code, (DecisionState.REJECT, "reject_code_inconnu")
    )
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
    
    # B-1 : ajouter l'advisory de conflit macro si pertinent
    if macro_conflict:
        advisories = advisories + (macro_conflict,)

    # R-5 FIX : ajouter l'advisory de positionnement IPS extrême si pertinent
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


def decide_all(
    desk: DeskSnapshot, macro: MacroSnapshot, *, include_rejects: bool = True,
    now: datetime | None = None,
) -> tuple[Decision, ...]:
    """Applique decide_setup a tous les setups valides du desk et, par
    defaut, decide_rejection a tous les rejets desk — invariant souverain
    (len(desk.setups) + len(desk.rejected) == desk.universe_total decisions).

    include_rejects=False restaure l'ancien comportement (setups valides
    seuls) pour compatibilite explicite, opt-in — jamais le defaut
    silencieux.

    now=None (defaut) preserve la purete de la fonction pour les golden
    files : la garde de fraicheur documentaire (audit B-2) ne s'active que
    si l'appelant fournit explicitement l'horloge (cf. cli.py)."""
    # PATCH-B4 (round du 31/07/2026, audit F-03/B-4) : quand le canal
    # directionnel macro est vide, le garde-fou _macro_priority_conflict et
    # le producteur currency_level_advisories sont structurellement inertes
    # — "Advisories : aucun" signifie alors "aucune these macro n'existait",
    # pas "aucun conflit macro". Doit etre declare dans le rapport final.
    if not macro.priority_setups:
        logger.warning(
            "macro_priority_setups_empty — canal directionnel macro INERTE ce cycle : "
            "garde _macro_priority_conflict et advisories currency-level desactives. "
            "Le rapport final doit le declarer (audit B-4)."
        )

    decisions = [decide_setup(s, macro, desk.correlation_groups, now=now) for s in desk.setups]

    # PATCH-FRESHNESS (round du 31/07/2026, audit B-2/F-04) : un document
    # desk perime ne doit plus pouvoir produire des decisions setups sans
    # marquage. Opt-in via now= pour preserver la purete/golden files.
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
        # PATCH-CLUSTERDUP-XREF (round du 31/07/2026, audit C-12) : un
        # doublon deduplique par le Desk reapparaissait en WATCH au meme
        # niveau visuel que son representant, sans que le lecteur puisse
        # voir que les deux coexistent. Advisory de contre-reference,
        # sans changement d'etat (zero-regression golden files).
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
