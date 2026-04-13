"""
Apex Tradovate Bot — TradingView → Tradovate API (CME Micro ETH Futures)
========================================================================
Règles Apex Trader Funding :
  - Clôture OBLIGATOIRE avant 22h45 Paris chaque soir (16h45 EDT)
  - Blackout overnight : 22h45 → 00h00 Paris
  - Blackout weekend  : vendredi 22h45 → lundi 00h00 Paris
  - Violation = échec immédiat du compte
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify

# =============================================================================
#  CONFIGURATION
# =============================================================================
TRADOVATE_EMAIL    = os.environ.get("TRADOVATE_EMAIL", "")
TRADOVATE_PASSWORD = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID   = os.environ.get("TRADOVATE_APP_ID", "Sample App")
TRADOVATE_APP_VERSION = os.environ.get("TRADOVATE_APP_VERSION", "1.0")
WEBHOOK_TOKEN      = os.environ.get("WEBHOOK_TOKEN", "apex_bot_secret_2026")

# Mode simulation (DRY_RUN) — mettre False pour le live
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# Contrat Micro ETH CME — se met à jour au rollover
CONTRACT_NAME = os.environ.get("CONTRACT_NAME", "METM6")  # Micro ETH Juin 2026

# Nombre de contrats par défaut (~35 pour $50k Apex)
DEFAULT_CONTRACTS = int(os.environ.get("DEFAULT_CONTRACTS", "35"))

# URLs Tradovate
TRADOVATE_DEMO_URL = "https://demo-api.tradovate.com/v1"
TRADOVATE_LIVE_URL = "https://live-api-d.tradovate.com/v1"
TRADOVATE_BASE_URL = os.environ.get("TRADOVATE_URL", TRADOVATE_DEMO_URL)

# Timezone Paris
TZ_PARIS = timezone(timedelta(hours=2))  # CEST (été) — à ajuster en hiver (+1)

# =============================================================================
#  LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("apex_orders.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# =============================================================================
#  ÉTAT INTERNE
# =============================================================================
_access_token    = None
_token_expiry    = 0
_open_position   = None   # {"side": "buy/sell", "contracts": N, "entry": price, "ts": ...}
_trade_log       = []
_daily_close_done = False
_last_close_date  = None

# =============================================================================
#  RÈGLES APEX — VÉRIFICATION HORAIRE
# =============================================================================

def _now_paris() -> datetime:
    return datetime.now(TZ_PARIS)

def _is_blackout() -> tuple[bool, str]:
    """Retourne (blackout_actif, raison)"""
    now = _now_paris()
    h, m, wd = now.hour, now.minute, now.weekday()  # 0=lundi, 4=vendredi, 5=samedi, 6=dimanche

    # Weekend : vendredi 22h45 → lundi 00h00
    if wd == 4 and (h > 22 or (h == 22 and m >= 45)):
        return True, "blackout_weekend_vendredi"
    if wd in (5, 6):
        return True, "blackout_weekend"
    if wd == 0 and h == 0:
        return True, "blackout_weekend_fin"

    # Overnight : 22h45 → 23h59
    if h == 22 and m >= 45:
        return True, "blackout_overnight_22h45"
    if h == 23:
        return True, "blackout_overnight_23h"

    return False, ""

def _must_close_now() -> bool:
    """True si on est dans la fenêtre de clôture obligatoire (22h30-22h45)"""
    now = _now_paris()
    wd = now.weekday()
    # Du lundi au vendredi entre 22h30 et 22h45
    if wd < 5 and now.hour == 22 and 30 <= now.minute < 45:
        return True
    return False

def _is_trading_allowed() -> tuple[bool, str]:
    blackout, reason = _is_blackout()
    if blackout:
        return False, reason
    return True, "ok"

# =============================================================================
#  TRADOVATE API — AUTHENTIFICATION
# =============================================================================

def _get_token() -> str | None:
    global _access_token, _token_expiry
    if _access_token and time.time() < _token_expiry - 60:
        return _access_token
    if not TRADOVATE_EMAIL or not TRADOVATE_PASSWORD:
        log.warning("Credentials Tradovate manquants (TRADOVATE_EMAIL / TRADOVATE_PASSWORD)")
        return None
    try:
        resp = requests.post(
            f"{TRADOVATE_BASE_URL}/auth/accesstokenrequest",
            json={
                "name":       TRADOVATE_EMAIL,
                "password":   TRADOVATE_PASSWORD,
                "appId":      TRADOVATE_APP_ID,
                "appVersion": TRADOVATE_APP_VERSION,
                "cid":        int(os.environ.get("TRADOVATE_CID", "0")),
                "sec":        os.environ.get("TRADOVATE_SEC", ""),
            },
            timeout=10
        )
        data = resp.json()
        if "accessToken" in data:
            _access_token = data["accessToken"]
            _token_expiry = time.time() + data.get("expirationTime", 3600) / 1000
            log.info(f"[Tradovate] Token obtenu, expire dans {data.get('expirationTime', 3600)/1000:.0f}s")
            return _access_token
        else:
            log.error(f"[Tradovate] Auth échouée : {data}")
            return None
    except Exception as e:
        log.error(f"[Tradovate] Auth exception : {e}")
        return None

def _tv_headers() -> dict:
    token = _get_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# =============================================================================
#  TRADOVATE API — ORDRES
# =============================================================================

def _get_contract_id(name: str) -> int | None:
    """Résout le nom du contrat (ex: METM6) en ID Tradovate."""
    try:
        resp = requests.get(
            f"{TRADOVATE_BASE_URL}/contract/find",
            params={"name": name},
            headers=_tv_headers(),
            timeout=10
        )
        data = resp.json()
        if isinstance(data, dict) and "id" in data:
            return data["id"]
        log.error(f"[Tradovate] Contrat {name} non trouvé : {data}")
        return None
    except Exception as e:
        log.error(f"[Tradovate] get_contract_id({name}) : {e}")
        return None

def _place_order(side: str, contracts: int, order_type: str = "Market") -> dict:
    """Place un ordre Market sur Tradovate."""
    if DRY_RUN:
        result = {"status": "dry_run", "side": side, "contracts": contracts,
                  "contract": CONTRACT_NAME, "ts": _now_paris().isoformat()}
        log.info(f"[DRY_RUN] Ordre simulé : {result}")
        return result

    contract_id = _get_contract_id(CONTRACT_NAME)
    if not contract_id:
        return {"status": "error", "reason": f"Contrat {CONTRACT_NAME} introuvable"}

    is_buy = side.lower() == "buy"
    action = "Buy" if is_buy else "Sell"

    try:
        resp = requests.post(
            f"{TRADOVATE_BASE_URL}/order/placeorder",
            json={
                "accountSpec":     TRADOVATE_EMAIL,
                "accountId":       int(os.environ.get("TRADOVATE_ACCOUNT_ID", "0")),
                "action":          action,
                "symbol":          CONTRACT_NAME,
                "orderQty":        contracts,
                "orderType":       order_type,
                "isAutomated":     True,
            },
            headers=_tv_headers(),
            timeout=10
        )
        data = resp.json()
        log.info(f"[Tradovate] Ordre {action} {contracts}x {CONTRACT_NAME} : {data}")
        return data
    except Exception as e:
        log.error(f"[Tradovate] Ordre échoué : {e}")
        return {"status": "error", "reason": str(e)}

def _close_position(reason: str = "manual") -> dict:
    """Ferme la position ouverte."""
    global _open_position
    if _open_position is None:
        log.info(f"[Tradovate] Pas de position à fermer ({reason})")
        return {"status": "no_position"}

    pos  = _open_position
    side_close = "sell" if pos["side"] == "buy" else "buy"
    log.info(f"[Tradovate] Clôture {reason} : {side_close} {pos['contracts']}x {CONTRACT_NAME}")

    result = _place_order(side_close, pos["contracts"])

    # Log trade
    _trade_log.append({
        "ts_open":   pos["ts"],
        "ts_close":  _now_paris().isoformat(),
        "side":      pos["side"],
        "contracts": pos["contracts"],
        "entry":     pos.get("entry", 0),
        "reason":    reason,
        "result":    result,
    })

    _open_position = None
    return result

# =============================================================================
#  TIMER — CLÔTURE AUTOMATIQUE APEX
# =============================================================================

def _daily_close_monitor():
    """Thread background — surveille l'heure et force la clôture avant 22h45 Paris."""
    global _daily_close_done, _last_close_date
    log.info("[APEX_TIMER] Démarrage surveillance clôture journalière")
    while True:
        try:
            now   = _now_paris()
            today = now.strftime("%Y-%m-%d")

            if _must_close_now() and _last_close_date != today:
                if _open_position:
                    log.warning(f"[APEX_TIMER] ⏰ 22h30-22h45 Paris — clôture obligatoire Apex !")
                    _close_position("apex_daily_close")
                    _last_close_date = today
                else:
                    _last_close_date = today
                    log.info(f"[APEX_TIMER] ✅ 22h30-22h45 — aucune position ouverte")

            # Reset à minuit
            if now.hour == 0 and now.minute == 1 and _last_close_date == today:
                _daily_close_done = False

        except Exception as e:
            log.error(f"[APEX_TIMER] Erreur : {e}")
        time.sleep(30)

# =============================================================================
#  ENDPOINTS FLASK
# =============================================================================

@app.route("/webhook/apex_bot_secret_2026", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principal — reçoit les alertes TradingView 45M ETH."""
    token_in_path = request.path == "/webhook/apex_bot_secret_2026"
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if not token_in_path and token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = json.loads(request.get_data(as_text=True))
    except Exception:
        return jsonify({"status": "ignored", "reason": "not json"}), 200

    action = data.get("action", "").lower()
    log.info(f"Signal reçu : {json.dumps(data)}")

    # ── Vérification horaire Apex ──
    allowed, reason = _is_trading_allowed()
    if not allowed and action == "open":
        log.warning(f"🚫 Signal bloqué — {reason}")
        return jsonify({"status": "blocked", "reason": reason}), 200

    if action == "open":
        return _handle_open(data)
    elif action == "close":
        result = _close_position(reason="signal_tv")
        return jsonify(result), 200
    else:
        return jsonify({"status": "ignored", "action": action}), 200


def _handle_open(data: dict):
    global _open_position
    side      = data.get("side", "").lower()
    contracts = int(data.get("contracts", DEFAULT_CONTRACTS))
    entry_px  = float(data.get("price", 0))
    regime    = data.get("regime", "?")

    log.info(f"\n{'='*50}")
    log.info(f"{'LONG' if side=='buy' else 'SHORT'} {CONTRACT_NAME} x{contracts} "
             f"@ {entry_px} | regime={regime}")

    # Position existante ?
    if _open_position:
        existing_side = _open_position["side"]
        if existing_side == side:
            log.warning(f"Position {side} déjà ouverte — signal ignoré")
            return jsonify({"status": "skipped", "reason": "same_direction"}), 200
        else:
            log.info(f"Retournement {existing_side} → {side} — clôture d'abord")
            _close_position("retournement")

    result = _place_order(side, contracts)

    if result.get("status") in ("dry_run", "ok") or "orderId" in result:
        _open_position = {
            "side":      side,
            "contracts": contracts,
            "entry":     entry_px,
            "ts":        _now_paris().isoformat(),
            "regime":    regime,
        }
        log.info(f"✅ Position ouverte : {_open_position}")

    return jsonify({"status": "received", "result": result}), 200


@app.route("/status", methods=["GET"])
def status():
    allowed, reason = _is_trading_allowed()
    return jsonify({
        "status":         "online",
        "mode":           "DRY_RUN" if DRY_RUN else "LIVE",
        "contract":       CONTRACT_NAME,
        "contracts_size": DEFAULT_CONTRACTS,
        "open_position":  _open_position,
        "trading_allowed": allowed,
        "blackout_reason": reason if not allowed else None,
        "time_paris":     _now_paris().strftime("%Y-%m-%d %H:%M:%S"),
        "time_utc":       datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/close", methods=["POST"])
def manual_close():
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    result = _close_position("manual")
    return jsonify(result), 200


@app.route("/trade_log", methods=["GET"])
def trade_log():
    return jsonify({"count": len(_trade_log), "trades": _trade_log}), 200


# =============================================================================
#  DÉMARRAGE
# =============================================================================

# Thread surveillance clôture Apex
threading.Thread(target=_daily_close_monitor, daemon=True, name="apex-timer").start()
log.info(f"Apex Tradovate Bot démarré")
log.info(f"Mode    : {'DRY_RUN' if DRY_RUN else 'LIVE'}")
log.info(f"Contrat : {CONTRACT_NAME} x{DEFAULT_CONTRACTS}")
log.info(f"Règles  : clôture 22h45 Paris, blackout overnight + weekend")
