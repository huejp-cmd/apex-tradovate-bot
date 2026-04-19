"""
APEX Labouchere Tracker — JP simplified system
===============================================
1 unite = $50 fixe
Sequence depart : [1, 1, 1, 1]
WIN  -> ajoute mise courante (ou [2,2,2] si mise >= seuil)
LOSS -> retire premier + dernier (reset si vide)
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
_PERSIST_DIR = os.environ.get("PERSIST_DIR", "/data")

def _resolve_path(filename):
    for d in [_PERSIST_DIR, "/app", "/tmp", _HERE]:
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".test")
            open(t, "w").write("x"); os.remove(t)
            return os.path.join(d, filename)
        except Exception:
            continue
    return os.path.join(_HERE, filename)

STATE_FILE   = _resolve_path("apex_lab_state.json")
HISTORY_FILE = _resolve_path("apex_lab_history.json")

UNIT_VALUE   = 1.0    # valeurs de sequence en $ directs
SPLIT_BET    = 300    # seuil split WIN -> [100,100,100] si mise >= $300
INIT_SEQ     = [50, 50, 50, 50]  # sequence depart : mise initiale = 50+50 = $100

_lock = threading.Lock()

# ============================================================
#  STATE
# ============================================================

def _default_state():
    return {
        "sequence":   list(INIT_SEQ),
        "wins":       0,
        "losses":     0,
        "cycles":     0,
        "cumulative_pnl": 0.0,
        "daily_pnl":  0.0,
        "daily_date": None,
    }

def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return _default_state()

def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _load_history() -> List[dict]:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _save_history(history: List[dict]):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

# ============================================================
#  LOGIC
# ============================================================

def _get_bet(seq: list) -> int:
    if not seq:  return 2
    if len(seq) == 1: return seq[0] * 2
    return seq[0] + seq[-1]

def _now_paris() -> str:
    paris = timezone(timedelta(hours=2))  # CEST
    return datetime.now(paris).strftime("%Y-%m-%d %H:%M")

def _today_paris() -> str:
    paris = timezone(timedelta(hours=2))
    return datetime.now(paris).strftime("%Y-%m-%d")

def record_result(side: str, coin: str, entry: float, exit_price: float,
                  result: str, pnl: float, signal_info: dict = None) -> dict:
    """
    result = "WIN" ou "LOSS"
    Retourne l'entree de journal creee.
    """
    with _lock:
        state   = _load_state()
        history = _load_history()

        seq_before = list(state["sequence"])
        bet        = _get_bet(seq_before)
        risk_usd   = bet * UNIT_VALUE
        reset      = False

        # Reset journalier
        today = _today_paris()
        if state.get("daily_date") != today:
            state["daily_pnl"]  = 0.0
            state["daily_date"] = today

        if result == "WIN":
            state["wins"] += 1
            if bet >= SPLIT_BET:
                state["sequence"].extend([100, 100, 100])
                added = [100, 100, 100]
            else:
                state["sequence"].append(bet)
                added = [bet]
        else:
            state["losses"] += 1
            seq = state["sequence"]
            if len(seq) >= 2:
                state["sequence"] = seq[1:-1]
            elif len(seq) == 1:
                state["sequence"] = []
            if not state["sequence"]:
                state["sequence"] = list(INIT_SEQ)
                state["cycles"]  += 1
                reset = True
            added = []

        seq_after = list(state["sequence"])
        bet_after = _get_bet(seq_after)

        state["cumulative_pnl"] += pnl
        state["daily_pnl"]      += pnl

        entry_log = {
            "ts":          _now_paris(),
            "date":        today,
            "coin":        coin,
            "side":        side,
            "entry":       entry,
            "exit":        exit_price,
            "result":      result,
            "pnl":         round(pnl, 2),
            "seq_before":  seq_before,
            "bet_before":  bet,
            "risk_usd":    risk_usd,
            "seq_after":   seq_after,
            "bet_after":   bet_after,
            "added":       added,
            "reset":       reset,
            "daily_pnl":   round(state["daily_pnl"], 2),
            "cum_pnl":     round(state["cumulative_pnl"], 2),
            "cum_wins":    state["wins"],
            "cum_losses":  state["losses"],
            "cycles":      state["cycles"],
            "signal_info": signal_info or {},
        }
        history.append(entry_log)
        _save_state(state)
        _save_history(history)
        return entry_log

CYCLE_RISK_USD = 200.0  # Fixe : 2 pertes x $100 pour vider [1,1,1,1] et reset

# ============================================================
#  WRAPPERS compatibles apex_tradovate_bot.py
# ============================================================

def get_current_bet() -> float:
    """Retourne la mise courante en dollars."""
    with _lock:
        state = _load_state()
        return float(_get_bet(state["sequence"]))

def get_state() -> dict:
    """Alias de get_current_state()."""
    return get_current_state()

def record_win(pnl: float) -> None:
    """Enregistre un gain (wrapper simplifie)."""
    record_result("bot", "MNQ", 0.0, 0.0, "WIN", pnl)

def record_loss(pnl: float) -> None:
    """Enregistre une perte (wrapper simplifie)."""
    record_result("bot", "MNQ", 0.0, 0.0, "LOSS", pnl)

def get_current_state() -> dict:
    with _lock:
        state = _load_state()
        seq   = state["sequence"]
        bet   = _get_bet(seq)
        total = state["wins"] + state["losses"]
        wr    = round(state["wins"] / total * 100, 1) if total > 0 else 0.0
        # Elements ajoutes par les gains (au-dela des 4 initiaux)
        extra = max(0, len(seq) - len(INIT_SEQ))
        return {
            "sequence":       seq,
            "bet_units":      bet,
            "risk_trade_usd": bet * UNIT_VALUE,
            "risk_cycle_usd": CYCLE_RISK_USD,
            "seq_elements":   len(seq),
            "seq_extra":      extra,
            "wins":           state["wins"],
            "losses":         state["losses"],
            "win_rate":       wr,
            "cycles":         state["cycles"],
            "daily_pnl":      round(state.get("daily_pnl", 0.0), 2),
            "cum_pnl":        round(state["cumulative_pnl"], 2),
            "unit_value":     UNIT_VALUE,
        }

def get_history(date: str = None, limit: int = 100) -> List[dict]:
    with _lock:
        h = _load_history()
        if date:
            h = [e for e in h if e.get("date") == date]
        return h[-limit:]

def get_daily_summary() -> List[dict]:
    with _lock:
        h = _load_history()
        by_day: Dict[str, dict] = {}
        for e in h:
            d = e.get("date","?")
            if d not in by_day:
                by_day[d] = {"date": d, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
            by_day[d]["trades"]  += 1
            by_day[d]["pnl"]     += e["pnl"]
            if e["result"] == "WIN":
                by_day[d]["wins"]   += 1
            else:
                by_day[d]["losses"] += 1
        result = sorted(by_day.values(), key=lambda x: x["date"], reverse=True)
        for r in result:
            r["pnl"] = round(r["pnl"], 2)
            r["wr"]  = round(r["wins"] / r["trades"] * 100, 1) if r["trades"] > 0 else 0.0
        return result

def reset_state():
    with _lock:
        _save_state(_default_state())

# ============================================================
#  HTML DASHBOARD
# ============================================================

def render_dashboard() -> str:
    state   = get_current_state()
    history = get_history(limit=200)
    today   = _today_paris()
    summary = get_daily_summary()

    seq       = state["sequence"]
    seq_html  = " | ".join(f"<b>{x}</b>" for x in seq) if seq else "<em>vide</em>"
    bet       = state["bet_units"]
    risk_t    = state["risk_trade_usd"]
    risk_c    = state["risk_cycle_usd"]
    cum       = state["cum_pnl"]
    dpnl      = state["daily_pnl"]
    wr        = state["win_rate"]
    w         = state["wins"]
    l         = state["losses"]
    cyc       = state["cycles"]
    n_elem    = state["seq_elements"]
    n_extra   = state["seq_extra"]

    cum_color  = "#4caf50" if cum  >= 0 else "#f44336"
    dpnl_color = "#4caf50" if dpnl >= 0 else "#f44336"

    # Tableau trades du jour
    today_trades = [e for e in history if e.get("date") == today]
    rows_today = ""
    for e in reversed(today_trades):
        r     = e["result"]
        rc    = "#4caf50" if r == "WIN" else "#f44336"
        pnl   = e["pnl"]
        pc    = "#4caf50" if pnl >= 0 else "#f44336"
        sb    = " | ".join(str(x) for x in e["seq_before"])
        sa    = " | ".join(str(x) for x in e["seq_after"])
        reset_tag = " <span style='color:#ff9800;font-size:11px'>[RESET]</span>" if e.get("reset") else ""
        added_str = ""
        if e.get("added"):
            if r == "WIN":
                added_str = f"<br><small style='color:#8bc34a'>+{e['added']} ajoutes</small>"
            else:
                added_str = f"<br><small style='color:#ef9a9a'>1er+dernier barres</small>"
        rows_today += f"""
        <tr>
          <td style='color:#aaa;font-size:12px'>{e['ts'][11:]}</td>
          <td>{e['coin']}</td>
          <td style='color:{"#4caf50" if e["side"]=="buy" else "#f44336"}'>{e['side'].upper()}</td>
          <td style='color:{rc};font-weight:bold'>{r}{reset_tag}</td>
          <td><span style='color:#90caf9;font-size:12px'>[{sb}]</span><br>
              <small style='color:#666'>mise={e["bet_before"]}u = ${e["risk_usd"]}</small>
              {added_str}</td>
          <td><span style='color:#b3e5fc;font-size:12px'>[{sa}]</span><br>
              <small style='color:#666'>prochain={e["bet_after"]}u = ${e["bet_after"]*50}</small></td>
          <td style='color:{pc};font-weight:bold'>${pnl:+.0f}</td>
          <td style='color:{"#4caf50" if e["cum_pnl"]>=0 else "#f44336"}'>${e["cum_pnl"]:+,.0f}</td>
        </tr>"""

    # Tableau historique journalier
    rows_daily = ""
    for d in summary[:14]:
        dc = "#4caf50" if d["pnl"] >= 0 else "#f44336"
        rows_daily += f"""
        <tr>
          <td>{'<b style="color:#fff">'+d['date']+'</b>' if d['date']==today else d['date']}</td>
          <td>{d['trades']}</td>
          <td style='color:#4caf50'>{d['wins']}</td>
          <td style='color:#f44336'>{d['losses']}</td>
          <td>{d['wr']}%</td>
          <td style='color:{dc};font-weight:bold'>${d['pnl']:+,.0f}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Labouchere APEX — JP</title>
<style>
  body{{background:#0d1117;color:#c9d1d9;font-family:monospace;margin:0;padding:16px}}
  h1{{color:#58a6ff;font-size:20px;margin:0 0 16px}}
  h2{{color:#8b949e;font-size:14px;margin:16px 0 8px;border-bottom:1px solid #21262d;padding-bottom:4px}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
  .stat{{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:12px;text-align:center}}
  .stat .val{{font-size:22px;font-weight:bold;margin:4px 0}}
  .stat .lbl{{font-size:11px;color:#8b949e}}
  .seq-box{{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;
            font-size:18px;letter-spacing:4px;text-align:center;margin:12px 0}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{background:#161b22;color:#8b949e;padding:6px 8px;text-align:left;border-bottom:1px solid #21262d}}
  td{{padding:5px 8px;border-bottom:1px solid #0d1117}}
  tr:hover td{{background:#161b22}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold}}
  .btn{{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:8px 16px;
        border-radius:6px;cursor:pointer;font-family:monospace;font-size:13px;margin:4px}}
  .btn:hover{{background:#30363d}}
  .btn-win{{color:#4caf50;border-color:#4caf50}}
  .btn-loss{{color:#f44336;border-color:#f44336}}
  .btn-reset{{color:#ff9800;border-color:#ff9800}}
  small{{color:#6e7681}}
</style>
</head>
<body>
<h1>Labouchere APEX &mdash; JP &nbsp;&nbsp;<small style='font-size:13px;color:#8b949e'>1 unite = $50 | Auto-refresh 30s</small></h1>

<div class="card">
  <h2>Sequence en cours</h2>
  <div class="seq-box" style="color:#58a6ff">[&nbsp;{seq_html}&nbsp;]</div>
  <div class="grid">
    <div class="stat">
      <div class="val" style="color:#ffd54f">{bet} u</div>
      <div class="lbl">Mise courante</div>
    </div>
    <div class="stat">
      <div class="val" style="color:#ffd54f">${risk_t:.0f}</div>
      <div class="lbl">Risque par trade</div>
    </div>
    <div class="stat" style="border-color:#ff9800">
      <div class="val" style="color:#ff9800">${risk_c:.0f}</div>
      <div class="lbl">Risque cycle (fixe)</div>
      <div style="font-size:10px;color:#666">2 pertes &times; $100 | {n_extra} elem. bonus gain</div>
    </div>
    <div class="stat">
      <div class="val" style="color:{dpnl_color}">${dpnl:+,.0f}</div>
      <div class="lbl">P&L Aujourd'hui</div>
    </div>
    <div class="stat">
      <div class="val" style="color:{cum_color}">${cum:+,.0f}</div>
      <div class="lbl">P&L Cumulatif</div>
    </div>
    <div class="stat">
      <div class="val" style="color:#4caf50">{w}W</div>
      <div class="lbl">Victoires</div>
    </div>
    <div class="stat">
      <div class="val" style="color:#f44336">{l}L</div>
      <div class="lbl">Defaites</div>
    </div>
    <div class="stat">
      <div class="val">{wr}%</div>
      <div class="lbl">Win Rate</div>
    </div>
    <div class="stat">
      <div class="val" style="color:#ff9800">{cyc}</div>
      <div class="lbl">Resets cycle</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Test manuel</h2>
  <form action="/labouchere/manual" method="post" style="display:inline">
    <input type="hidden" name="result" value="WIN">
    <input type="hidden" name="coin" value="ETH">
    <input type="hidden" name="side" value="buy">
    <input type="hidden" name="pnl" value="200">
    <button type="submit" class="btn btn-win">+ WIN +$200</button>
  </form>
  <form action="/labouchere/manual" method="post" style="display:inline">
    <input type="hidden" name="result" value="LOSS">
    <input type="hidden" name="coin" value="ETH">
    <input type="hidden" name="side" value="buy">
    <input type="hidden" name="pnl" value="-100">
    <button type="submit" class="btn btn-loss">- LOSS -$100</button>
  </form>
  <form action="/labouchere/reset" method="post" style="display:inline"
        onsubmit="return confirm('Reinitialiser toute la sequence ?')">
    <button type="submit" class="btn btn-reset">Reset sequence</button>
  </form>
</div>

<div class="card">
  <h2>Trades d'aujourd'hui &mdash; {today}</h2>
  {'<p style="color:#8b949e;font-style:italic">Aucun trade ce jour</p>' if not today_trades else f'''
  <table>
    <tr>
      <th>Heure</th><th>Coin</th><th>Cote</th><th>Resultat</th>
      <th>Sequence avant</th><th>Sequence apres</th><th>P&L</th><th>Cumul</th>
    </tr>
    {rows_today}
  </table>'''}
</div>

<div class="card">
  <h2>Historique journalier (14 derniers jours)</h2>
  {'<p style="color:#8b949e;font-style:italic">Aucun historique</p>' if not summary else f'''
  <table>
    <tr><th>Date</th><th>Trades</th><th>W</th><th>L</th><th>Win%</th><th>P&L</th></tr>
    {rows_daily}
  </table>'''}
</div>

<p style="color:#444;font-size:11px;text-align:center">
  Labouchere APEX JP &mdash; Mise a jour toutes les 30s &mdash; {_now_paris()} (Paris)
</p>
</body>
</html>"""
