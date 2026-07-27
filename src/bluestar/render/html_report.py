"""
Rendu HTML du rapport de comité Bluestar.

Principe architectural : ce module est un CONSOMMATEUR PASSIF du schéma de
décision. Il ne contient aucune logique de décision — il ne fait que mettre en
forme des objets `Decision` déjà calculés par `bluestar.decide.selection_grid`.
Aucune ligne de tableau n'est écrite à la main : tout est généré depuis les
données réelles, setup par setup.

Le design (header BLUESTAR, palette, disposition) reprend à l'identique celui
validé manuellement dans les itérations précédentes : étoile bleu royal
(#1B45B4) sur fond blanc — jamais l'inverse.
"""

from __future__ import annotations

import html as html_lib
import logging
from datetime import datetime, timezone

from bluestar.decide.selection_grid import AssetClass, Decision, DecisionState, LegVerdict
from bluestar.errors import RenderError
from bluestar.models import DeskSnapshot, MacroSnapshot

logger = logging.getLogger("bluestar.render")

_STATE_BADGE_CLASS = {
    DecisionState.ELIGIBLE: "b-eligible",
    DecisionState.WATCH: "b-watch",
    DecisionState.REJECT: "b-reject",
    DecisionState.BLOCKED_DATA: "b-blocked",
    DecisionState.BLOCKED_RISK: "b-blocked",
}

_VERDICT_CLASS = {
    LegVerdict.CONFLUENCE: "v-confluence",
    LegVerdict.CONFLIT: "v-conflit",
    LegVerdict.NEUTRE: "v-neutre",
    LegVerdict.INDETERMINE: "v-indetermine",
}

_VERDICT_LABEL = {
    LegVerdict.CONFLUENCE: "Confluence",
    LegVerdict.CONFLIT: "Conflit",
    LegVerdict.NEUTRE: "Neutre",
    LegVerdict.INDETERMINE: "Indéterminé",
}

_STATE_ORDER = {
    DecisionState.ELIGIBLE: 0,
    DecisionState.WATCH: 1,
    DecisionState.BLOCKED_DATA: 2,
    DecisionState.BLOCKED_RISK: 3,
    DecisionState.REJECT: 4,
}


def _esc(text: str) -> str:
    return html_lib.escape(str(text), quote=True)


_ASSET_BADGE_LABEL = {
    AssetClass.EQUITY_INDEX: "INDICE - JAMBE UNIQUE",
    AssetClass.METAL: "METAL",
    AssetClass.OTHER: "NON CLASSIFIE",
}


def _render_row(d: Decision) -> str:
    badge_class = _STATE_BADGE_CLASS[d.state]
    legs_html = ""
    if d.legs:
        for leg in d.legs:
            vclass = _VERDICT_CLASS[leg.verdict]
            vlabel = _VERDICT_LABEL[leg.verdict]
            legs_html += (
                f'<span class="verdict-line"><span class="{vclass}">{_esc(leg.currency)}</span> '
                f'{vlabel} — {_esc(leg.detail)}</span>'
            )
    else:
        legs_html = '<span class="verdict-line"><span class="v-neutre">—</span> Non applicable / non évalué</span>'

    advisories_html = ""
    if d.advisories:
        items = "".join(f"<li>{_esc(a)}</li>" for a in d.advisories)
        advisories_html = f'<ul class="advisory-list">{items}</ul>'

    if d.direction is None:
        dir_class, dir_arrow = "dir-neutral", "◆ N/A"
    elif d.direction.value == "long":
        dir_class, dir_arrow = "dir-long", "▲ LONG"
    else:
        dir_class, dir_arrow = "dir-short", "▼ SHORT"

    asset_badge_html = ""
    if d.asset_class != AssetClass.FX_PAIR:
        label = _ASSET_BADGE_LABEL.get(d.asset_class, d.asset_class.value)
        asset_badge_html = f'<br><span class="badge badge-asset">{_esc(label)}</span>'

    source_code_html = _esc(d.source_reject_code) if d.source_reject_code else '<span class="muted">—</span>'

    return f"""
      <tr>
        <td><span class="pair">{_esc(d.pair)}</span><br><span class="{dir_class}">{dir_arrow}</span>{asset_badge_html}</td>
        <td>{legs_html}</td>
        <td class="detail">{advisories_html if advisories_html else '<span class="muted">aucun</span>'}</td>
        <td><span class="badge {badge_class}">{_esc(d.state.value)}</span></td>
        <td class="factor">{_esc(d.limiting_factor)}</td>
        <td class="detail">{source_code_html}</td>
      </tr>"""


_CSS = """
:root{
  --font-mono:'DejaVu Sans Mono','Consolas','Liberation Mono',monospace;
  --font-sans:'DejaVu Sans','Segoe UI',Helvetica,Arial,sans-serif;
  --royal:#1B45B4;--royal-dark:#0D1F4E;
  --green:#0EA968;--green-soft:#E9FBF3;
  --amber:#D97B15;--amber-soft:#FDF3E4;
  --red:#DC2626;--red-soft:#FDECEC;
  --slate:#5B6B94;--bg:#F4F6FC;--card:#FFFFFF;--border:#E1E7F5;
  --blocked:#3F1D1D;--blocked-soft:#F3E5E5;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);font-family:var(--font-sans);color:var(--royal-dark)}
#page{max-width:1160px;margin:0 auto;padding:0 18px 28px}
#pdf-fab{position:fixed;top:16px;right:16px;z-index:50;text-align:right}
#pdf-fab button{font-family:var(--font-mono);font-size:11px;font-weight:700;padding:10px 16px;
  border-radius:8px;cursor:pointer;border:none;background:var(--royal);color:#fff;letter-spacing:.3px}
#pdf-fab button:hover{background:var(--royal-dark)}
.page-header{display:flex;justify-content:space-between;align-items:center;
  padding:20px 0 14px;border-bottom:3px solid var(--royal-dark)}
.header-left{display:flex;align-items:center;gap:12px}
.logo-marker{width:42px;height:42px;border-radius:5px;background:#FFFFFF;border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.sys-label{font-family:var(--font-mono);font-size:9px;color:var(--slate);letter-spacing:1px;text-transform:uppercase}
.sys-name{font-family:var(--font-mono);font-size:19px;font-weight:700;color:var(--royal-dark);letter-spacing:.5px}
.sys-desc{font-family:var(--font-mono);font-size:9.5px;color:var(--royal);letter-spacing:.4px;margin-top:1px}
.header-right{text-align:right}
.briefing-label{font-family:var(--font-mono);font-size:11px;font-weight:700;color:var(--royal);
  letter-spacing:.5px;text-transform:uppercase}
.briefing-sub{font-size:10.5px;color:var(--slate);margin-top:2px}
.page-subbar{display:flex;gap:18px;flex-wrap:wrap;padding:9px 0;font-family:var(--font-mono);
  font-size:10px;color:var(--slate);border-bottom:1px solid var(--border);margin-bottom:20px}
.page-subbar .confidential{color:var(--red);font-weight:700;letter-spacing:.4px}
.meta-dates{font-family:var(--font-mono);font-size:10px;color:var(--slate);
  background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:16px;line-height:1.8}
.meta-dates b{color:var(--royal-dark)}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:13px 16px}
.kpi .lbl{font-family:var(--font-mono);font-size:9px;text-transform:uppercase;color:var(--slate);letter-spacing:.6px}
.kpi .val{font-family:var(--font-mono);font-size:21px;font-weight:700;color:var(--royal);margin-top:5px}
.kpi .hint{font-size:10px;color:var(--slate);margin-top:3px}
.kpi.top{border-top:3px solid var(--green)}
.kpi.mid{border-top:3px solid var(--amber)}
.kpi.low{border-top:3px solid var(--blocked)}
.kpi.royal{border-top:3px solid var(--royal)}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);
      border:1px solid var(--border);border-radius:12px;overflow:hidden;
      box-shadow:0 4px 14px rgba(13,31,78,.06)}
th{background:var(--royal-dark);color:#fff;font-family:var(--font-mono);font-size:9.5px;
   text-align:left;padding:11px 12px;letter-spacing:.3px;text-transform:uppercase}
td{padding:12px 14px;font-size:11.5px;border-top:1px solid var(--border);vertical-align:top}
tr:nth-child(even) td{background:#FAFBFF}
.pair{font-family:var(--font-mono);font-weight:700;font-size:14px}
.dir-long{color:var(--green);font-size:10px;font-family:var(--font-mono)}
.dir-short{color:var(--red);font-size:10px;font-family:var(--font-mono)}
.dir-neutral{color:var(--slate);font-size:10px;font-family:var(--font-mono)}
.badge{font-family:var(--font-mono);font-size:9px;padding:3px 8px;border-radius:5px;
       display:inline-block;font-weight:700;letter-spacing:.3px}
.badge-asset{background:#EEF1FA;color:var(--slate);margin-top:3px}
.b-eligible{background:var(--green-soft);color:var(--green)}
.b-watch{background:var(--amber-soft);color:var(--amber)}
.b-reject{background:var(--red-soft);color:var(--red)}
.b-blocked{background:var(--blocked-soft);color:var(--blocked)}
.verdict-line{display:block;margin-bottom:3px;font-size:10.5px}
.v-confluence{color:var(--green);font-weight:700}
.v-conflit{color:var(--red);font-weight:700}
.v-neutre{color:var(--slate);font-weight:700}
.v-indetermine{color:var(--amber);font-weight:700}
.detail{font-size:10.5px;color:var(--slate);line-height:1.55}
.muted{color:var(--slate);font-style:italic}
.advisory-list{margin:0;padding-left:14px;line-height:1.6}
.factor{font-size:11px;font-weight:700;color:var(--royal-dark)}
.footer-note{margin-top:16px;font-size:10px;color:var(--slate);font-family:var(--font-mono);
  border-top:1px solid var(--border);padding-top:10px}
@media print{
  @page{size:landscape;margin:8mm}
  body{padding:6px;background:#fff}
  #pdf-fab{display:none}
  td,th{font-size:9px;padding:6px}
  table,.kpi{box-shadow:none}
}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}}
@media(max-width:1040px) and (min-width:761px){.kpis{grid-template-columns:repeat(3,1fr)}}
"""


def render_report(desk: DeskSnapshot, macro: MacroSnapshot, decisions: tuple[Decision, ...],
                   generated_at: datetime | None = None) -> str:
    """Génère le rapport HTML complet. Fonction quasi pure : seule dépendance
    externe est l'horodatage de génération (injectable pour les tests)."""
    if not decisions:
        raise RenderError("Aucune décision à rendre — decisions est vide.")

    generated_at = generated_at or datetime.now(timezone.utc)

    ordered = sorted(decisions, key=lambda d: _STATE_ORDER[d.state])

    counts = {s: 0 for s in DecisionState}
    for d in ordered:
        counts[d.state] += 1

    rows_html = "\n".join(_render_row(d) for d in ordered)

    eligible_pairs = ", ".join(d.pair for d in ordered if d.state == DecisionState.ELIGIBLE) or "aucun"
    watch_pairs = ", ".join(d.pair for d in ordered if d.state == DecisionState.WATCH) or "aucun"
    blocked_pairs = ", ".join(
        d.pair for d in ordered if d.state in (DecisionState.BLOCKED_DATA, DecisionState.BLOCKED_RISK)
    ) or "aucun"

    total_advisories = sum(len(d.advisories) for d in ordered)

    grid_version = ordered[0].grid_version

    html_out = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Comité de Sélection — BLUESTAR</title>
<style>{_CSS}</style>
</head>
<body>
<div id="pdf-fab"><button onclick="window.print()">📥 Télécharger PDF</button></div>
<div id="page">
  <div class="page-header">
    <div class="header-left">
      <div class="logo-marker">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 17.27L18.18 21L16.54 13.97L22 9.24L14.81 8.63L12 2L9.19 8.63L2 9.24L7.46 13.97L5.82 21L12 17.27Z" fill="#1B45B4"/>
        </svg>
      </div>
      <div>
        <div class="sys-label">BLUESTAR SYSTEM</div>
        <div class="sys-name">BLUESTAR</div>
        <div class="sys-desc">COMITÉ DE SÉLECTION — CROISEMENT MACRO × TECHNIQUE</div>
      </div>
    </div>
    <div class="header-right">
      <div class="briefing-label">RAPPORT DE DÉCISION</div>
      <div class="briefing-sub">Généré le {_esc(generated_at.strftime('%Y-%m-%d %H:%M UTC'))}</div>
    </div>
  </div>
  <div class="page-subbar">
    <span>📅 Doc macro : {_esc(macro.report_datetime)} {_esc(macro.report_timezone)}</span>
    <span>📄 Doc desk : {_esc(desk.report_datetime)} {_esc(desk.report_timezone)}</span>
    <span class="confidential">● CONFIDENTIEL</span>
  </div>

  <div class="meta-dates">
    Régime macro : <b>{_esc(macro.regime)}</b> (confiance {_esc(macro.regime_confidence_pct)}%) ·
    Univers desk : <b>{desk.universe_evaluated}/{desk.universe_total}</b>,
    {len(desk.setups)} setups validés, {len(desk.rejected)} rejetés ·
    Décisions comité : <b>{len(ordered)}/{desk.universe_total}</b> ·
    Grille de décision : <b>{_esc(grid_version)}</b>
  </div>

  <div class="kpis">
    <div class="kpi royal"><div class="lbl">Univers traité</div><div class="val">{len(ordered)}</div><div class="hint">sur {desk.universe_total} actifs desk</div></div>
    <div class="kpi top"><div class="lbl">ELIGIBLE</div><div class="val">{counts[DecisionState.ELIGIBLE]}</div><div class="hint">{_esc(eligible_pairs)}</div></div>
    <div class="kpi mid"><div class="lbl">WATCH</div><div class="val">{counts[DecisionState.WATCH]}</div><div class="hint">{_esc(watch_pairs)}</div></div>
    <div class="kpi low"><div class="lbl">BLOCKED</div><div class="val">{counts[DecisionState.BLOCKED_DATA] + counts[DecisionState.BLOCKED_RISK]}</div><div class="hint">{_esc(blocked_pairs)}</div></div>
    <div class="kpi low"><div class="lbl">REJECT</div><div class="val">{counts[DecisionState.REJECT]}</div><div class="hint">rejets desk + jambe unique</div></div>
    <div class="kpi royal"><div class="lbl">Advisories</div><div class="val">{total_advisories}</div><div class="hint">signaux non bloquants</div></div>
  </div>

  <table>
    <thead><tr>
      <th>Setup</th><th>Verdict par jambe</th><th>Advisories (non bloquants)</th>
      <th>Décision</th><th>Facteur limitant réel</th><th>Code rejet desk</th>
    </tr></thead>
    <tbody>{rows_html}
    </tbody>
  </table>

  <div class="footer-note">
    ELIGIBLE ≠ EXECUTER : ce rapport sort du moteur d'éligibilité seul. Toute exécution
    réelle nécessite en aval un moteur de portefeuille (exposition, corrélation,
    plafond de positions) hors périmètre de ce document. Les advisories sont des
    signaux informatifs qui n'ont jamais modifié un état — leur éventuelle
    escalade en règle bloquante est une décision de gouvernance humaine, pas
    une inférence automatique.
  </div>
</div>
</body>
</html>"""

    logger.info("report_rendered setups=%d eligible=%d watch=%d blocked=%d advisories=%d",
                len(ordered), counts[DecisionState.ELIGIBLE], counts[DecisionState.WATCH],
                counts[DecisionState.BLOCKED_DATA] + counts[DecisionState.BLOCKED_RISK],
                total_advisories)
    return html_out
