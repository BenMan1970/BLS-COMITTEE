"""Schéma de données typé pour Bluestar v2.

Principe architectural (Partie 4.2 de la spécification) : le HTML n'est jamais
une source de vérité. Ces dataclasses SONT la source de vérité à partir du moment
où extract/ les a produites. decide/ ne connaît que ces objets, jamais le HTML.

Toutes les classes sont immuables (frozen=True), y compris le CONTENU de leurs
champs de type mapping (currencies, factors, correlation_groups,
calendar_coverage), pas seulement l'attribut lui-même.
Correction du 19/07/2026 (défaut trouvé par l'audit GPT-5.5, confirmé par
exécution) : `frozen=True` seul empêche `snapshot.currencies = x` mais pas
`snapshot.currencies["EUR"] = x` sur un dict ordinaire. Les champs mapping sont
enveloppés dans `types.MappingProxyType` via `__post_init__`, qui lève
`TypeError` sur toute tentative d'écriture, y compris après construction.

ICF v2 (04/08/2026) — champs ajoutés, tous OPTIONNELS avec défaut neutre :
  - DeskSetup.cap_reason      (Proposition 1)
  - DeskSetup.factors_missing (Proposition 7 — plomberie seule)
  - DeskSnapshot.calendar_coverage (Proposition 2)
  - DeskSnapshot.macro_regime_label (Proposition 6)
Ces champs étaient DÉJÀ extraits par bluestar.extract.desk_parser mais jetés à
la frontière faute de place dans le modèle : les cascades défensives du parser
retombaient en schéma dégradé avec un simple logger.info. Zéro régression :
un document qui ne les porte pas produit exactement les valeurs par défaut
ci-dessous, donc exactement le comportement d'avant ce patch.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class IPSZone(str, Enum):
    CAPITULATION = "capitulation"
    NORMAL = "normal"
    CROWDED = "crowded"


IPS_CAPITULATION_MAX = 20.0
IPS_CROWDED_MIN = 80.0


def ips_zone(value: float | None) -> IPSZone | None:
    if value is None:
        return None
    if value < IPS_CAPITULATION_MAX:
        return IPSZone.CAPITULATION
    if value > IPS_CROWDED_MIN:
        return IPSZone.CROWDED
    return IPSZone.NORMAL


@dataclass(frozen=True)
class CurrencyMacroData:
    code: str
    strength_rank: int | None
    strength_score: float | None
    ips: float | None
    ips_source: str | None
    ips_date: str | None

    @property
    def zone(self) -> IPSZone | None:
        return ips_zone(self.ips)


@dataclass(frozen=True)
class MacroPrioritySetup:
    pair: str
    direction: Direction
    conviction_stars: int
    rationale: str


@dataclass(frozen=True)
class MacroSnapshot:
    report_datetime: str
    report_timezone: str
    regime: str
    regime_confidence_pct: float | None
    currencies: Mapping[str, CurrencyMacroData]
    priority_setups: tuple[MacroPrioritySetup, ...]
    extreme_currency_count: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "currencies", types.MappingProxyType(dict(self.currencies)))


@dataclass(frozen=True)
class FlagRef:
    code: str
    severity: str
    detail: str = ""


@dataclass(frozen=True)
class DeskSetup:
    """Un setup validé par le desk technique (un bloc .setup du rapport)."""
    pair: str
    direction: Direction
    conviction_grade: str
    conviction_value: float
    cluster_tag: str
    quality: str | None
    mtf_pct: float | None
    age_days: int | None
    risk_reward: float | None
    factors: Mapping[str, float]
    entry: float | None
    stop_loss: float | None
    flags: tuple = ()
    cal_status: str | None = None
    cal_note: str = ""
    # X-9/G6 FIX (02/08/2026) : type d'entrée calculé par le Desk ("Market" ou
    # "Limit"), rendu dans le HTML source (`.entry .px-sub`). Nécessaire pour
    # qualifier une entrée "Market" générée marché FX fermé (gate G6).
    entry_type: str | None = None
    # PATCH-CAPREASON (ICF v2, Proposition 1, 04/08/2026) : motif du plafond de
    # conviction posé par le Desk (`<div class="cap-note">`). N'alimente QU'UN
    # enrichissement du `limiting_factor` affiché au Comité — jamais un état,
    # jamais un score, jamais un gate. Absence légitime (setup non plafonné)
    # -> None, jamais une erreur.
    cap_reason: str | None = None
    # PATCH-FACTORSMISS (ICF v2, Proposition 7, 04/08/2026) : ensemble des clés
    # de `factors` dont la valeur porte la classe CSS `miss` côté Desk, c.-à-d.
    # NON MESURÉES (et donc exclues de `absolute_mean` par le moteur Desk).
    # Sans ce champ, un F4=0.00 mesuré et un F4=0.00 non mesuré sont
    # indiscernables côté Comité (audit C-6).
    # PLOMBERIE ET DIVULGATION UNIQUEMENT : aucune règle de décision ne
    # consomme ce champ à ce jour, délibérément (toute règle du type "pas
    # d'ELIGIBLE sans trigger mesuré" est une décision de gouvernance, à
    # traiter dans un patch séparé avec fixture dédiée).
    factors_missing: frozenset[str] = frozenset()
    # HARNESS P3-3 (2026-09-11, redite relevée par le SET 10/09) : le Desk
    # publie la grille complète Entry/SL/TP1/TP2/RR (cinq `px-card`) ; le
    # Comité extrayait déjà entry/stop_loss/rr mais JETAIT TP1/TP2 (aucun
    # champ) et ne rendait AUCUN niveau — une décision sans prix exécutable.
    # Plomberie/transport pur : aucun gate ne consomme ces champs ; les états
    # produits sont bit-à-bit indépendants (golden rejoué vérifié).
    take_profit_1: float | None = None
    take_profit_2: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "factors", types.MappingProxyType(dict(self.factors)))
        object.__setattr__(self, "factors_missing", frozenset(self.factors_missing))

    def leg_currencies(self) -> tuple[str, str] | None:
        if "/" not in self.pair:
            return None
        # PATCH-P2-SPLITGUARD (audit forensique 09/09, AC-09) : une paire
        # contenant 2+ « / » levait un ValueError non rattrapé (« too many
        # values to unpack » → code CLI 5, chemin traceback). Une paire
        # indéchiffrable n'est pas un crash : c'est un instrument sans
        # jambes au sens de la grille, traité par la branche asset_class.
        parts = self.pair.split("/")
        if len(parts) != 2:
            return None
        base, quote = parts
        if not (len(base) == 3 and base.isalpha() and len(quote) == 3 and quote.isalpha()):
            return None
        if self.direction == Direction.LONG:
            return (base, quote)
        return (quote, base)


@dataclass(frozen=True)
class DeskRejectedSetup:
    pair: str
    direction: Direction | None
    reject_code: str
    detail: str


@dataclass(frozen=True)
class CorrelationSignal:
    symbol: str
    direction: Direction | None
    kind: str
    timeframe: str
    mtf_pct: float | None
    quality: str
    confluence: float | None


@dataclass(frozen=True)
class DeskSnapshot:
    """État complet du rapport desk à un instant donné (point-in-time)."""
    report_datetime: str
    report_timezone: str
    universe_evaluated: int
    universe_total: int
    event_risk: str
    themes: tuple[str, ...]
    setups: tuple[DeskSetup, ...]
    rejected: tuple[DeskRejectedSetup, ...]
    correlation_groups: Mapping[str, tuple[CorrelationSignal, ...]] = field(default_factory=dict)
    # K-3/R-3/G4 FIX (02/08/2026) : bannières document-niveau du Desk (fuseau
    # incohérent, couverture calendrier tronquée — `<div class="banner">`).
    banners: tuple[str, ...] = ()
    # PATCH-CALCOVERAGE (ICF v2, Proposition 2, 04/08/2026) : couverture
    # calendaire déclarée par le Desk, sous la forme
    # {"covered": frozenset(...), "uncovered": frozenset(...)}.
    # Le Desk le calcule déjà et l'énonce en prose dans une bannière ; ce champ
    # transporte la version STRUCTURÉE (bloc <script id="calendar-coverage">),
    # seule forme joignable à une ligne de décision. Dict vide = information
    # absente du document (comportement strictement identique à avant).
    calendar_coverage: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # HARNESS P3-2 (boucle de cohérence harnais -> comité) : le bloc
    # `calendar-coverage` du producteur déclare aussi l'INTEGRITE du flux
    # (`truncated`, `feed_end_utc`, `horizon_h`) — la bannière du document le
    # dit en prose, mais le consommateur jetait le signal structuré : un
    # horizon tronqué se lisait comme une couverture simplement « complète ».
    # DIVULGATION PURE : aucune règle de décision ne consomme ce champ ; il
    # n'enrichit que la ligne document-niveau de couverture. Document ancien
    # ou bloc sans ces clés -> dict vide, rendu strictement identique à avant.
    calendar_coverage_meta: Mapping[str, str] = field(default_factory=dict)
    # P3-4 (arbitrage écrit 11/09/2026, « on corrige P3 ») : occurrences de
    # devises VISIBLES dans le briefing calendaire du Desk (`cal-brief` :
    # lignes `cal-tbl` + bande « Hors book »). TRI-ÉTAT délibéré :
    #   None  = document sans briefing calendaire visible — on NE PEUT RIEN
    #           conclure : l'advisory P3-4 reste muette ;
    #   {}    = briefing calendaire rendu mais AUCUNE occurrence — vrai
    #           silence affiché, matière de l'advisory « silence non
    #           vérifiable » ;
    #   {...} = comptage par devise (une occurrence visible = 1).
    # Plomberie et divulgation uniquement : AUCUNE règle d'état ne consomme
    # ce champ ; il n'alimente que l'advisory NON bloquante P3-4.
    calendar_occurrences: Mapping[str, int] | None = None
    # PATCH-DUALREGIME (ICF v2, Proposition 6, 04/08/2026) : "régime" propre au
    # Desk (état CALENDAIRE, ex. EVENT_DRIFT / POST_POLICY_REPRICING), distinct
    # du "régime" du Macro (état de MARCHÉ, ex. Mixed / Selective). Deux
    # constructions différentes portant le même mot — Règle Absolue 4 : on les
    # nomme distinctement au rendu. Divulgation pure, consommé par aucune règle.
    macro_regime_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "correlation_groups", types.MappingProxyType(dict(self.correlation_groups))
        )
        object.__setattr__(
            self, "calendar_coverage", types.MappingProxyType(dict(self.calendar_coverage))
        )

    @property
    def uncovered_currencies(self) -> frozenset[str]:
        """Devises absentes du flux calendrier du Desk. frozenset() vide si
        l'information n'est pas présente dans le document — ce qui signifie
        « non déclaré », jamais « tout est couvert »."""
        return frozenset(self.calendar_coverage.get("uncovered", frozenset()))

    @property
    def covered_currencies(self) -> frozenset[str]:
        return frozenset(self.calendar_coverage.get("covered", frozenset()))
