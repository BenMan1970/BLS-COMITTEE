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


# ---------------------------------------------------------------------------
# SEUILS IPS — SOURCE UNIQUE DE VÉRITÉ.
#
# Défaut corrigé (round d'audit du 19/07/2026, trouvé indépendamment par
# l'audit GPT-5.5, manqué par Claude 4.8 et Kimi K2) : ces seuils étaient
# auparavant DUPLIQUÉS — une copie déclarée dans decide/selection_grid.py
# sous les mêmes noms (IPS_CAPITULATION_MAX, IPS_CROWDED_MIN), présentée
# comme "registre gelé", mais jamais lue par ips_zone() ci-dessous, qui
# utilisait ses propres littéraux 20/80 codés en dur. Modifier la copie de
# selection_grid.py n'avait donc AUCUN effet sur la décision réelle.
#
# Ces deux constantes sont maintenant définies UNE seule fois, ici — le seul
# endroit où la logique de zonage IPS s'exécute réellement — et
# decide/selection_grid.py les importe plutôt que de les redéclarer.
# Toute autre duplication future doit être considérée comme un bug, pas une
# clarification.
# ---------------------------------------------------------------------------
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
    """Une devise vue par le briefing macro : force relative + positionnement."""
    code: str
    strength_rank: int | None          # position dans le classement (1 = plus fort)
    strength_score: float | None       # score 0-100
    ips: float | None                  # Institutional Positioning Score 0-100
    ips_source: str | None
    ips_date: str | None

    @property
    def zone(self) -> IPSZone | None:
        return ips_zone(self.ips)


@dataclass(frozen=True)
class MacroPrioritySetup:
    """Un setup mis en avant explicitement par le briefing macro (fiche actif)."""
    pair: str
    direction: Direction
    conviction_stars: int
    rationale: str


@dataclass(frozen=True)
class MacroSnapshot:
    """État complet du briefing macro à un instant donné (point-in-time)."""
    report_datetime: str
    report_timezone: str
    regime: str
    regime_confidence_pct: float | None
    currencies: Mapping[str, CurrencyMacroData]
    priority_setups: tuple[MacroPrioritySetup, ...]
    extreme_currency_count: int | None  # comptage annoncé par le doc, pour cross-check

    def __post_init__(self) -> None:
        # frozen=True empêche `self.currencies = x` (réassignation), mais PAS
        # `self.currencies["EUR"] = x` si currencies est un dict ordinaire
        # (mutation du contenu, pas de l'attribut) — vérifié par exécution
        # lors du round d'audit du 19/07/2026 (trouvé par l'audit GPT-5.5).
        # MappingProxyType ferme ce trou : toute écriture sur le mapping lève
        # TypeError, y compris après construction.
        object.__setattr__(self, "currencies", types.MappingProxyType(dict(self.currencies)))


@dataclass(frozen=True)
class FlagRef:
    """Flag de contradiction (C1-C10) émis par le moteur Desk.
    PATCH-B1 (audit B-1/C-02, round 31/07/2026) : structuration des flags
    pour rétablir le canal Desk → Comité."""
    code: str
    severity: str          # "minor" | "major"
    detail: str = ""


@dataclass(frozen=True)
class DeskSetup:
    """Un setup validé par le desk technique (un bloc .setup du rapport)."""
    pair: str
    direction: Direction
    conviction_grade: str               # ex: "BBB" — échelle propre au desk
    conviction_value: float             # ex: 0.77
    cluster_tag: str
    quality: str | None                 # ex: "A+"
    mtf_pct: float | None
    age_days: int | None
    risk_reward: float | None
    factors: Mapping[str, float]        # F1 HWA, F2 RMG, ... F7 MAC, Q-rang
    entry: float | None
    stop_loss: float | None
    # PATCH-B1 (audit B-1/C-02, round 31/07/2026) : fin de la perte silencieuse
    # des flags et statuts calendaires. Défauts assurant une zero-régression
    # si le parser ne fournit pas ces champs (viellles versions de HTML).
    flags: tuple = ()                   # tuple[FlagRef|dict, ...] — dicts acceptés (parser émet des dicts)
    cal_status: str | None = None       # "OK"|"PROXIMITY"|"WATCH"|"BLACKOUT"
    cal_note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "factors", types.MappingProxyType(dict(self.factors)))

    def leg_currencies(self) -> tuple[str, str] | None:
        """Décompose une paire FX en (devise_longue, devise_courte).
        Retourne None si l'instrument n'est pas une paire à deux devises
        FX reconnues. Un code devise valide = exactement 3 lettres (ISO 4217-like).
        'US30' (4 caractères, contient un chiffre) échoue ce test et retourne
        donc None : c'est un indice actions, pas une paire FX, même si son
        symbole contient un '/'. C'est précisément le garde-fou que le comité
        d'audit a identifié comme manquant (mode 'jambe unique' requis)."""
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
    """Un signal technique (CHoCH etc.) associé à une devise, tel que
    calculé en amont par le moteur (merged_pipeline.json::correlation_groups)
    et transporté jusqu'au HTML desk depuis le round du 27/07/2026.
    Purement informatif — jamais utilisé pour changer un état de décision,
    même contrat que les autres advisories."""
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
    # PATCH-CORRGROUPS (round du 28/07/2026) : dict devise -> signaux
    # techniques réels (pas une thèse macro déduite). Vide par défaut pour
    # tout document desk antérieur au correctif moteur qui l'embarque.
    correlation_groups: dict[str, tuple[CorrelationSignal, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "correlation_groups", types.MappingProxyType(dict(self.correlation_groups))
        )
