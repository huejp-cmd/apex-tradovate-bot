"""
Apex V8 HL Bot — TradingView → Tradovate CME (Webhook Server)
=============================================================
Architecture :
  TradingView (Pine V8 HL 1H ETH) → Webhook JSON
    → Flask server (ce fichier)
      → CMEGuardian (vérifie horaires + Apex rules)
      → ApexLabouchereV8 (calcule bet_units + contrats METH)
      → Tradovate API (place l'ordre)
      → apex_v8hl_history.json (journalise)

Stratégie : V8 HL (HullMA+ADX+RSI+VWAP+BB) sur METH 1H
Sizing    : Labouchere V8 HL
  - Mode validation  : [2,2,2,2] × $75/unité → validation $3k en ~3j
  - Mode PA normal   : [2,2,2,2] × $50/unité
  - Mode PA conserv  : [1,1,1,1] × $50/unité

Règles CME / Apex (toutes vérifiées avant chaque trade) :
  - Clôture auto 22h30 Paris (avant limite 22h45)
  - Blackout overnight + weekend
  - Daily loss limit $2 500 (soft stop $1 500)
  - Daily profit cap $1 500 (protège trailing drawdown)
  - Trailing DD guard ($2 500)

Format signal TradingView attendu (alerte Pine Script) :
  {
    "action":    "open" | "close" | "status",
    "side":      "buy" | "sell",
    "price":     1823.45,
    "atr_sl":    67.8,
    "regime":    "trend" | "explosive",
    "token":     "apex_v8hl_secret"
  }
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify, render_template_string

import apex_labouchere_v8 as lab
from cme_guardian import guardian, CMEGuardian

# ─────────────────────────────────────────────────────────────────
#  Configuration (variables d'environnement)
# ─────────────────────────────────────────────────────────────────
TRADOVATE_EMAIL      = os.environ.get("TRADOVATE_EMAIL", "sumiko")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "Sample App")
TRADOVATE_APP_VER    = os.environ.get("TRADOVATE_APP_VERSION", "1.0")
TRADOVATE_CID        = int(os.environ.get("TRADOVATE_CID", "0"))
TRADOVATE_SEC        = os.environ.get("TRADOVATE_SEC", "")
TRADOVATE_ACCOUNT_ID = int(os.environ.get("TRADOVATE_ACCOUNT_ID", "0"))
WEBHOOK_TOKEN        = os.environ.get("WEBHOOK_TOKEN", "apex_v8hl_secret_2026")

# Mode simulation
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# Contrat CME actif (rollover manuel à chaque expiration)
# METH = Micro ETH, suffixe = mois+année (H=mars, M=juin, U=sept, Z=déc)
CONTRACT_NAME = os.environ.get("CONTRACT_NAME", "METM6")  # Micro ETH Juin 2026

# URL Tradovate (demo pour test, live pour réel)
TRADOVATE_DEMO_URL = "https://demo-api.tradovate.com/v1"
TRADOVATE_LIVE_URL = "https://live-api-d.tradovate.com/v1"
TRADOVATE_BASE_URL = os.environ.get("TRADOVATE_URL", TRADOVATE_DEMO_URL)

# ─────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("apex_v8hl.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("apex_v8hl")

app = Flask(__name__)
TZ_PARIS = timezone(timedelta(hours=2))

# ─────────────────────────────────────────────────────────────────
#  État interne
# ─────────────────────────────────────────────────────────────────
_access_token   = None
_token_expiry   = 0
_open_position  = None   # {"side", "contracts", "entry", "atr_sl", "ts", "regime"}
_trade_log      = []
_signals_log    = []
_last_close_date = None

def _now_paris() -> datetime:
    return datetime.now(TZ_PARIS)

# ─────────────────────────────────────────────────────────────────
#  Tradovate API — Auth
# ─────────────────────────────────────────────────────────────────

def _get_token() -> str | None:
    global _access_token, _token_expiry
    if _access_token and time.time() < _token_expiry - 60:
        return _access_token
    if not TRADOVATE_EMAIL or not TRADOVATE_PASSWORD:
        log.warning("[Tradovate] Credentials manquants (env TRADOVATE_EMAIL / TRADOVATE_PASSWORD)")
        return None
    try:
        resp = requests.post(
            f"{TRADOVATE_BASE_URL}/auth/accesstokenrequest",
            json={
                "name":       TRADOVATE_EMAIL,
                "password":   TRADOVATE_PASSWORD,
                "appId":      TRADOVATE_APP_ID,
                "appVersion": TRADOVATE_APP_VER,
                "cid":        TRADOVATE_CID,
                "sec":        TRADOVATE_SEC,
            },
            timeout=10,
        )
        data = resp.json()
        if "accessToken" in data:
            _access_token = data["accessToken"]
            # expirationTime est en ms depuis epoch
            exp_ms = data.get("expirationTime", 0)
            _token_expiry = exp_ms / 1000 if exp_ms > 1e10 else time.time() + 3600
            log.info(f"[Tradovate] ✅ Token obtenu (expire {_token_expiry:.0f})")
            return _access_token
        else:
            log.error(f"[Tradovate] ❌ Auth échouée : {data}")
            return None
    except Exception as e:
        log.error(f"[Tradovate] Auth exception : {e}")
        return None

def _headers() -> dict:
    token = _get_token()
    if not token:
        raise RuntimeError("Pas de token Tradovate — vérifier les credentials")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ─────────────────────────────────────────────────────────────────
#  Tradovate API — Contrat
# ─────────────────────────────────────────────────────────────────

_contract_id_cache: dict = {}

def _get_contract_id(name: str) -> int | None:
    if name in _contract_id_cache:
        return _contract_id_cache[name]
    try:
        resp = requests.get(
            f"{TRADOVATE_BASE_URL}/contract/find",
            params={"name": name},
            headers=_headers(),
            timeout=10,
        )
        data = resp.json()
        if isinstance(data, dict) and "id" in data:
            _contract_id_cache[name] = data["id"]
            log.info(f"[Tradovate] Contrat {name} → ID {data['id']}")
            return data["id"]
        log.error(f"[Tradovate] Contrat {name} non trouvé : {data}")
        return None
    except Exception as e:
        log.error(f"[Tradovate] get_contract_id({name}) exception : {e}")
        return None

# ─────────────────────────────────────────────────────────────────
#  Tradovate API — Ordres
# ─────────────────────────────────────────────────────────────────

def _place_order(side: str, contracts: int, order_type: str = "Market") -> dict:
    """
    Place un ordre sur Tradovate.
    side = "buy" ou "sell"
    contracts = nombre de contrats METH (entier)
    """
    ts = _now_paris().isoformat()

    if DRY_RUN:
        result = {
            "status":    "dry_run",
            "side":      side,
            "contracts": contracts,
            "symbol":    CONTRACT_NAME,
            "ts":        ts,
        }
        log.info(f"[DRY_RUN] 🔵 Ordre simulé : {side.upper()} {contracts}x {CONTRACT_NAME}")
        return result

    try:
        contract_id = _get_contract_id(CONTRACT_NAME)
        if not contract_id:
            return {"status": "error", "reason": f"Contrat {CONTRACT_NAME} introuvable"}

        action = "Buy" if side.lower() == "buy" else "Sell"
        payload = {
            "accountSpec":  TRADOVATE_EMAIL,
            "accountId":    TRADOVATE_ACCOUNT_ID,
            "action":       action,
            "symbol":       CONTRACT_NAME,
            "orderQty":     contracts,
            "orderType":    order_type,
            "isAutomated":  True,
        }
        resp = requests.post(
            f"{TRADOVATE_BASE_URL}/order/placeorder",
            json=payload,
            headers=_headers(),
            timeout=10,
        )
        data = resp.json()
        log.info(f"[Tradovate] Ordre {action} {contracts}x {CONTRACT_NAME} → {data}")
        return data
    except Exception as e:
        log.error(f"[Tradovate] Place order exception : {e}")
        return {"status": "error", "reason": str(e)}


def _close_position(reason: str = "manual", exit_price: float = 0.0, pnl: float = 0.0) -> dict:
    """
    Ferme la position ouverte (ordre inverse).
    Enregistre le résultat dans le Labouchere.
    """
    global _open_position

    if _open_position is None:
        log.info(f"[Bot] Pas de position à fermer ({reason})")
        return {"status": "no_position"}

    pos        = _open_position
    side_close = "sell" if pos["side"] == "buy" else "buy"
    contracts  = pos["contracts"]

    log.info(f"[Bot] 🔴 Clôture {reason} : {side_close.upper()} {contracts}x {CONTRACT_NAME}")
    result = _place_order(side_close, contracts)

    # ── Mise à jour Labouchere ──
    # Pour les fermetures forcées (apex_daily_close, etc.), pnl=0 → LOSS (sécurité)
    is_win   = pnl > 0
    lab_result = "WIN" if is_win else "LOSS"

    entry_log = lab.record_result(
        side        = pos["side"],
        result      = lab_result,
        pnl         = pnl,
        contracts   = contracts,
        entry_price = pos.get("entry", 0),
        exit_price  = exit_price,
        atr_sl      = pos.get("atr_sl", 0),
        signal_info = {
            "regime": pos.get("regime", "?"),
            "reason": reason,
            "order":  result,
        },
    )

    _trade_log.append({
        "ts_open":    pos["ts"],
        "ts_close":   _now_paris().isoformat(),
        "side":       pos["side"],
        "contracts":  contracts,
        "entry":      pos.get("entry", 0),
        "exit":       exit_price,
        "pnl":        pnl,
        "reason":     reason,
        "lab_result": lab_result,
        "lab_entry":  entry_log,
        "order":      result,
    })

    _open_position = None
    log.info(f"[Bot] Position fermée | {lab_result} | P&L={pnl:+.0f}$ | "
             f"Seq après: {entry_log['seq_after']}")
    return {"status": "closed", "reason": reason, "pnl": pnl, "lab": entry_log}

# ─────────────────────────────────────────────────────────────────
#  Thread — Surveillance clôture Apex
# ─────────────────────────────────────────────────────────────────

def _apex_close_monitor():
    """
    Thread daemon — tourne toutes les 30s.
    Ferme la position si on approche 22h45 Paris ou si daily loss atteint.
    """
    global _last_close_date
    log.info("[ApexTimer] ⏱️  Surveillance clôture démarrée")
    while True:
        try:
            now   = _now_paris()
            today = now.strftime("%Y-%m-%d")
            must_close, reason = guardian.must_close_now()

            if must_close and _last_close_date != today:
                if _open_position:
                    log.warning(f"[ApexTimer] ⏰ CLÔTURE FORCÉE ({reason})")
                    _close_position(reason=reason, pnl=0.0)  # pnl inconnu → LOSS
                _last_close_date = today
                guardian.on_day_close(closing_balance=50_000)  # Idéalement depuis Tradovate account

            # Reset lundi matin
            if now.weekday() == 0 and now.hour == 0 and now.minute < 10:
                _last_close_date = None

        except Exception as e:
            log.error(f"[ApexTimer] Erreur : {e}")
        time.sleep(30)

# ─────────────────────────────────────────────────────────────────
#  Webhook — Handler principal
# ─────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
@app.route(f"/webhook/<token>", methods=["POST"])
def webhook(token: str = None):
    """
    Endpoint TradingView.
    Authentification : token dans l'URL ou header X-Webhook-Token.
    """
    # Auth
    header_token = request.headers.get("X-Webhook-Token")
    url_token    = token
    if header_token != WEBHOOK_TOKEN and url_token != WEBHOOK_TOKEN:
        log.warning(f"[Webhook] 🚫 Token invalide depuis {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = json.loads(request.get_data(as_text=True))
    except Exception:
        return jsonify({"status": "ignored", "reason": "not_json"}), 200

    action = data.get("action", "").lower()
    _signals_log.append({
        "ts":     _now_paris().isoformat(),
        "action": action,
        "data":   data,
    })
    if len(_signals_log) > 500:
        _signals_log[:] = _signals_log[-500:]

    log.info(f"[Webhook] Signal reçu : {action} | {json.dumps(data)}")

    if action == "open":
        return _handle_open(data)
    elif action == "close":
        price = float(data.get("price", 0))
        pnl   = float(data.get("pnl", 0))
        result = _close_position("signal_tv_close", exit_price=price, pnl=pnl)
        return jsonify(result), 200
    elif action == "status":
        return jsonify(_get_full_status()), 200
    else:
        return jsonify({"status": "ignored", "action": action}), 200


def _handle_open(data: dict):
    """Traite un signal d'ouverture."""
    global _open_position

    side       = data.get("side", "").lower()
    entry_px   = float(data.get("price", 0))
    atr_sl_usd = float(data.get("atr_sl", 60.0))   # SL en $ mouvement ETH
    regime     = data.get("regime", "trend")

    if side not in ("buy", "sell"):
        return jsonify({"status": "error", "reason": f"side invalide: {side}"}), 200

    # ── 1. Vérification rules CME + Apex ──
    allowed, reason = guardian.is_trading_allowed(current_balance=None)
    if not allowed:
        log.warning(f"[Bot] 🚫 Trade bloqué : {reason}")
        return jsonify({"status": "blocked", "reason": reason}), 200

    # Vérification Labouchere financière
    lab_state = lab.get_state_summary()
    lab_state_raw = lab._load_state()
    can_trade, reason = lab.check_can_trade(lab_state_raw, 50_000)
    if not can_trade:
        log.warning(f"[Bot] 🚫 Trade bloqué (Lab guard) : {reason}")
        return jsonify({"status": "blocked", "reason": reason}), 200

    # ── 2. Calcul sizing Labouchere ──
    bet_info  = lab.get_current_bet(atr_sl_usd=atr_sl_usd, eth_price=entry_px)
    contracts = bet_info["contracts"]
    bet_units = bet_info["bet_units"]
    risk_usd  = bet_info["risk_usd"]

    log.info(
        f"[Bot] 📊 Sizing V8 HL : {side.upper()} | "
        f"bet={bet_units}u=${risk_usd:.0f} | "
        f"contracts={contracts}x {CONTRACT_NAME} | "
        f"regime={regime} | ATR_SL=${atr_sl_usd:.1f}"
    )

    # ── 3. Gestion position existante ──
    if _open_position:
        existing = _open_position["side"]
        if existing == side:
            log.info(f"[Bot] Position {side} déjà ouverte — signal ignoré")
            return jsonify({"status": "skipped", "reason": "same_direction"}), 200
        else:
            log.info(f"[Bot] Retournement {existing}→{side} — clôture avant ouverture")
            _close_position("retournement", exit_price=entry_px, pnl=0.0)

    # ── 4. Placement ordre ──
    result = _place_order(side, contracts)

    order_ok = (
        DRY_RUN or
        result.get("status") == "ok" or
        "orderId" in result or
        "id" in result
    )

    if order_ok:
        _open_position = {
            "side":      side,
            "contracts": contracts,
            "entry":     entry_px,
            "atr_sl":    atr_sl_usd,
            "ts":        _now_paris().isoformat(),
            "regime":    regime,
            "bet_units": bet_units,
            "risk_usd":  risk_usd,
        }
        log.info(f"[Bot] ✅ Position ouverte : {_open_position}")

    return jsonify({
        "status":    "ok" if order_ok else "error",
        "side":      side,
        "contracts": contracts,
        "bet_units": bet_units,
        "risk_usd":  risk_usd,
        "dry_run":   DRY_RUN,
        "result":    result,
    }), 200

# ─────────────────────────────────────────────────────────────────
#  Dashboard — Status
# ─────────────────────────────────────────────────────────────────

def _get_full_status() -> dict:
    lab_s = lab.get_state_summary()
    g_s   = guardian.get_status()
    allowed, reason = guardian.is_trading_allowed()
    return {
        "bot": {
            "mode":           "DRY_RUN" if DRY_RUN else "LIVE",
            "contract":       CONTRACT_NAME,
            "open_position":  _open_position,
            "time_paris":     _now_paris().strftime("%Y-%m-%d %H:%M:%S CEST"),
            "trading_allowed": allowed,
            "block_reason":   reason if not allowed else None,
        },
        "labouchere": lab_s,
        "guardian":   g_s,
        "trade_count": len(_trade_log),
    }


@app.route("/status", methods=["GET"])
def status():
    return jsonify(_get_full_status()), 200


@app.route("/close", methods=["POST"])
def manual_close():
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    price = float(request.args.get("price", 0))
    pnl   = float(request.args.get("pnl", 0))
    result = _close_position("manual_close", exit_price=price, pnl=pnl)
    return jsonify(result), 200


@app.route("/lab/state", methods=["GET"])
def lab_state():
    return jsonify(lab.get_state_summary()), 200


@app.route("/lab/history", methods=["GET"])
def lab_history():
    d = request.args.get("date", None)
    return jsonify(lab.get_history(date_filter=d, limit=100)), 200


@app.route("/lab/manual", methods=["POST"])
def lab_manual():
    """Test manuel : enregistrer un WIN/LOSS pour tester la séquence."""
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data   = request.get_json(force=True)
    result = data.get("result", "WIN")
    pnl    = float(data.get("pnl", 200 if result == "WIN" else -150))
    entry  = lab.record_result(
        side="buy", result=result, pnl=pnl,
        contracts=1, entry_price=1800, exit_price=1850,
        atr_sl=60, signal_info={"source": "manual_test"},
    )
    return jsonify(entry), 200


@app.route("/lab/reset", methods=["POST"])
def lab_reset():
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    mode = request.args.get("mode", lab.MODE_VALIDATION)
    lab.reset_sequence(mode)
    return jsonify({"status": "reset", "mode": mode}), 200


@app.route("/lab/mode", methods=["POST"])
def lab_mode():
    """Change le mode (validation → pa_normal, pa_conserv)."""
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    mode = request.args.get("mode", lab.MODE_VALIDATION)
    lab.set_mode(mode)
    return jsonify({"status": "mode_changed", "mode": mode}), 200


@app.route("/signals", methods=["GET"])
def signals_log():
    limit = int(request.args.get("limit", 20))
    return jsonify(_signals_log[-limit:]), 200


@app.route("/trades", methods=["GET"])
def trades_log():
    return jsonify({"count": len(_trade_log), "trades": _trade_log[-50:]}), 200


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Dashboard HTML minimal."""
    s   = _get_full_status()
    lab_s = s["labouchere"]
    g_s   = s["guardian"]
    bot_s = s["bot"]

    pos_html = ""
    if bot_s["open_position"]:
        p = bot_s["open_position"]
        pos_html = f"""
        <div style="background:#1a2a1a;border:1px solid #4caf50;border-radius:6px;padding:12px;margin:12px 0">
          <b style="color:#4caf50">▶ Position ouverte</b>
          {p['side'].upper()} {p['contracts']}x {CONTRACT_NAME} @{p.get('entry',0):.2f}
          | bet={p.get('bet_units',0)}u=${p.get('risk_usd',0):.0f}
          | {p.get('regime','?')} | depuis {p['ts']}
        </div>"""

    allow_color = "#4caf50" if bot_s["trading_allowed"] else "#f44336"
    allow_txt   = "✅ TRADING AUTORISÉ" if bot_s["trading_allowed"] else f"🚫 BLOQUÉ — {bot_s['block_reason']}"

    seq_html = " | ".join(f"<b>{x}</b>" for x in lab_s['sequence']) if lab_s['sequence'] else "<i>vide</i>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta http-equiv="refresh" content="15">
<title>Apex V8 HL Bot</title>
<style>
body{{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:16px;margin:0}}
h1{{color:#58a6ff;font-size:18px}}
h2{{color:#8b949e;font-size:13px;border-bottom:1px solid #21262d;padding-bottom:4px;margin:16px 0 8px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin:10px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.stat{{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px;text-align:center}}
.val{{font-size:20px;font-weight:bold;margin:4px 0}}
.lbl{{font-size:11px;color:#8b949e}}
.seq{{background:#0d1117;border:1px solid #30363d;padding:10px;border-radius:6px;
      font-size:16px;letter-spacing:3px;text-align:center;margin:8px 0}}
</style></head><body>
<h1>🤖 Apex V8 HL Bot — METH (Micro ETH CME)</h1>
<div style="color:{allow_color};font-size:14px;margin:8px 0;font-weight:bold">{allow_txt}</div>
<small style="color:#666">{bot_s['time_paris']} | {bot_s['mode']} | {CONTRACT_NAME}</small>

{pos_html}

<div class="card">
  <h2>Séquence Labouchere — mode {lab_s['mode']}</h2>
  <div class="seq" style="color:#58a6ff">[&nbsp;{seq_html}&nbsp;]</div>
  <div class="grid">
    <div class="stat"><div class="val" style="color:#ffd54f">{lab_s['bet_units']}u</div><div class="lbl">Mise courante</div></div>
    <div class="stat"><div class="val" style="color:#ffd54f">${lab_s['risk_per_trade']:.0f}</div><div class="lbl">Risque/trade</div></div>
    <div class="stat"><div class="val" style="color:{'#4caf50' if lab_s['daily_pnl']>=0 else '#f44336'}">${lab_s['daily_pnl']:+,.0f}</div><div class="lbl">P&L Today</div></div>
    <div class="stat"><div class="val" style="color:{'#4caf50' if lab_s['cum_pnl']>=0 else '#f44336'}">${lab_s['cum_pnl']:+,.0f}</div><div class="lbl">P&L Cumul</div></div>
    <div class="stat"><div class="val" style="color:#4caf50">{lab_s['wins']}W</div><div class="lbl">Victoires</div></div>
    <div class="stat"><div class="val" style="color:#f44336">{lab_s['losses']}L</div><div class="lbl">Défaites</div></div>
    <div class="stat"><div class="val">{lab_s['win_rate']}%</div><div class="lbl">Win Rate</div></div>
    <div class="stat"><div class="val" style="color:#ff9800">{lab_s['cycles']}</div><div class="lbl">Resets</div></div>
    <div class="stat"><div class="val" style="color:#90caf9">{lab_s['epp_estimate']:+.0f}$</div><div class="lbl">EV/trade est.</div></div>
    <div class="stat"><div class="val" style="color:#90caf9">{lab_s['days_to_3k_est']}j</div><div class="lbl">Jours → $3k est.</div></div>
  </div>
</div>

<div class="card">
  <h2>Apex Trailing DD Guard</h2>
  <div class="grid">
    <div class="stat"><div class="val" style="color:#fff">${g_s['eod_hwm']:,.0f}</div><div class="lbl">EOD HWM</div></div>
    <div class="stat"><div class="val" style="color:#f44336">${g_s['dd_floor']:,.0f}</div><div class="lbl">Floor Trailing DD</div></div>
    <div class="stat"><div class="val" style="color:{'#4caf50' if g_s['floor_margin']>1000 else '#ff9800'}">${g_s['floor_margin']:,.0f}</div><div class="lbl">Marge DD</div></div>
    <div class="stat"><div class="val" style="color:{'#4caf50' if lab_s['daily_pnl']>=0 else '#f44336'}">${g_s['daily_pnl']:+,.0f}</div><div class="lbl">Daily P&L</div></div>
    <div class="stat"><div class="val" style="color:{'#ffd54f' if not g_s['target_reached'] else '#4caf50'}">{'✅ VALIDÉ' if g_s['target_reached'] else '$'+str(int(g_s['target_balance']))}</div><div class="lbl">Objectif</div></div>
  </div>
</div>

<p style="color:#444;font-size:11px;text-align:center">Auto-refresh 15s — {bot_s['time_paris']}</p>
</body></html>"""


# ─────────────────────────────────────────────────────────────────
#  Démarrage
# ─────────────────────────────────────────────────────────────────

# Thread surveillance clôture
threading.Thread(
    target=_apex_close_monitor, daemon=True, name="apex-close-monitor"
).start()

log.info("=" * 60)
log.info("🚀 Apex V8 HL Bot — démarré")
log.info(f"   Mode     : {'🔵 DRY_RUN' if DRY_RUN else '🔴 LIVE'}")
log.info(f"   Contrat  : {CONTRACT_NAME} (Micro ETH CME)")
log.info(f"   Lab mode : {lab.get_state_summary().get('mode')}")
log.info(f"   Unit $   : {lab.get_state_summary().get('unit_value')}/unité")
log.info(f"   Endpoint : POST /webhook (token requis)")
log.info("=" * 60)
