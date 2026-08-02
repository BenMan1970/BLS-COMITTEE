"""Schéma de données typé pour Bluestar v2.

Principe architectural (Partie 4.2 de la spécification) : le HTML n'est jamais
une source de vérité. Ces dataclasses SONT la source de vérité à partir du moment
où extract/ les a produites. decide/ ne connaît que ces objets, jamais le HTML.

Toutes les classes sont immuables (frozen=True), y compris le CONTENU de leurs
champs de type mapping (currencies, factors), pas seulement l'attribut lui-même.
Correction du 19/07/2026 (défaut trouvé par l'audit GPT-5.5, confirmé par
exécution) : `frozen=True` seul empêche `snapshot.currencies = x` mais pas
`snapshot.currencies["EUR"] = x` sur un dict ordinaire. Les champs mapping sont
maintenant enveloppés dans `types.MappingProxyType` via `__post_init__`, qui lève
`TypeError` sur toute tentative d'écriture, y compris après construction.
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
    # X-9/G6 FIX (round de validation zero-régression, 02/08/2026) : type
    # d'entrée calculé par le Desk ("Market" ou "Limit"), jusqu'ici jamais
    # transmis au Comité alors qu'il est rendu dans le HTML source
    # (`.entry .px-sub`). Nécessaire pour qualifier une entrée "Market"
    # générée marché FX fermé (gate G6) et, plus généralement, pour toute
    # logique qui doit distinguer un prix d'entrée "au marché" d'un niveau
    # de zone S/R. Défaut `None` : zéro régression pour tout document desk
    # antérieur à ce correctif (le parser ne l'extrait pas, ou une version
    # de bluestar.extract.desk_parser antérieure à ce correctif).
    entry_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "factors", types.MappingProxyType(dict(self.factors)))

    def leg_currencies(self) -> tuple[str, str] | None:
        if "/" not in self.pair:
            return None
        base, quote = self.pair.split("/")
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
    correlation_groups: dict[str, tuple[CorrelationSignal, ...]] = field(default_factory=dict)
    # K-3/R-3/G4 FIX (round de validation zero-régression, 02/08/2026) :
    # bannières document-niveau du Desk (fuseau incohérent, couverture
    # calendrier tronquée — `<div class="banner">` dans le HTML source),
    # jusqu'ici jamais transmises au Comité alors qu'elles existent bel et
    # bien dans le document. Défaut `()` : zéro régression pour tout
    # document desk antérieur à ce correctif.
    banners: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "correlation_groups", types.MappingProxyType(dict(self.correlation_groups))
        )
