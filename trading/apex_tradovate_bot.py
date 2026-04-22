"""
apex_tradovate_bot.py
---------------------
Serveur FastAPI — Bot de trading Apex Trader Funding via l'API Tradovate.

Lancement :
    uvicorn apex_tradovate_bot:app --host 0.0.0.0 --port 8080

Variables d'environnement requises :
    TRADOVATE_USERNAME      - Identifiant Tradovate
    TRADOVATE_PASSWORD      - Mot de passe Tradovate
    TRADOVATE_ACCOUNT_SPEC  - Ex: "MyAccount-12345"
    APEX_WEBHOOK_TOKEN      - Token secret pour valider les webhooks TradingView
    CONTRACT_SYMBOL         - Ex: "METH" ou "MNQ" (default: METH)
    UNIT_DOLLAR             - Valeur d'1 unité Labouchere en $ (default: 50)
"""

import asyncio
import logging
import os
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# ── Piliers techniques (2026-04-21) ─────────────────────────────────────────
# Pilier 1 : Persistance JSON/Redis
try:
    from state_manager import state_manager
    PILIER_PERSISTANCE = True
except ImportError:
    PILIER_PERSISTANCE = False
    logging.warning("state_manager.py introuvable — persistance désactivée.")
    class _FallbackSM:
        def save(self, *a, **kw): pass
        def load(self): return {}
        def update(self, **kw): pass
        def clear(self): pass
    state_manager = _FallbackSM()

# Pilier 4 : Notifications téléphone
try:
    from notifier import notify, notify_trade_open, notify_trade_close, notify_halt, notify_low_volatility
    PILIER_NOTIFIER = True
except ImportError:
    PILIER_NOTIFIER = False
    logging.warning("notifier.py introuvable — notifications désactivées.")
    async def notify(msg, urgent=False): logging.info(f"[NOTIFY] {msg}")
    async def notify_trade_open(*a, **kw): pass
    async def notify_trade_close(*a, **kw): pass
    async def notify_halt(*a, **kw): pass
    async def notify_low_volatility(*a, **kw): pass

# Pilier 2 : ATR Range Builder + WebSocket
try:
    from atr_range_builder import ATRRangeSelector, RangeBarBuilder
    from ws_manager import TradovateWSManager
    PILIER_WS = True
except ImportError:
    PILIER_WS = False
    logging.warning("ws_manager.py / atr_range_builder.py introuvables — WS désactivé.")
# ─────────────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# Import Labouchere tracker (module local existant)
# ---------------------------------------------------------------------------
try:
    from apex_lab_tracker import get_current_bet, get_state, record_loss, record_win
    LAB_AVAILABLE = True
except ImportError:
    LAB_AVAILABLE = False
    logging.warning("apex_lab_tracker.py introuvable — Labouchere désactivé.")

    def get_current_bet() -> float:
        return float(os.getenv("UNIT_DOLLAR", "50"))

    def record_win(pnl: float) -> None:
        pass

    def record_loss(pnl: float) -> None:
        pass

    def get_state() -> dict:
        return {"error": "apex_lab_tracker non disponible"}


# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("apex_bot")

TZ_PARIS = ZoneInfo("Europe/Paris")

TRADOVATE_BASE_URL = os.getenv("TRADOVATE_BASE_URL", "https://live.tradovateapi.com/v1")
TRADOVATE_USERNAME = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_ENV = os.getenv("TRADOVATE_ENV", "demo")  # "demo" pour eval Apex, "live" pour compte réel
TRADOVATE_ACCESS_TOKEN = os.getenv("TRADOVATE_ACCESS_TOKEN", "")  # Token pré-fourni (refresh automatique)
TRADOVATE_ACCOUNT_SPEC = os.getenv("TRADOVATE_ACCOUNT_SPEC", "")
APEX_WEBHOOK_TOKEN = os.getenv("APEX_WEBHOOK_TOKEN", "")
CONTRACT_SYMBOL = os.getenv("CONTRACT_SYMBOL", "MNQ")
UNIT_DOLLAR = float(os.getenv("UNIT_DOLLAR", "50"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")

# Règles Apex ($50k evaluation)
APEX_PROFIT_TARGET = 3000.0
APEX_MAX_DAILY_LOSS = 2500.0
APEX_TRAILING_DRAWDOWN = 2500.0
APEX_CONSISTENCY_MAX_DAY_PCT = 0.30  # 30% du profit total max en 1 journée

# Heure de force-close NQ/MNQ : 15h55 ET (5 min avant cloture CME 16h00)
# Pour METH/ETH : 22h45 Paris
CME_FORCE_CLOSE_HOUR = int(os.getenv("FORCE_CLOSE_HOUR_ET", "15"))
CME_FORCE_CLOSE_MINUTE = int(os.getenv("FORCE_CLOSE_MIN_ET", "55"))

# MNQ : $2 par point, SL fixe 9 points, TP fixe 18 points
MNQ_POINT_VALUE = 2.0
MNQ_SL_POINTS   = float(os.getenv("MNQ_SL_POINTS", "9"))
MNQ_TP_POINTS   = float(os.getenv("MNQ_TP_POINTS", "18"))


# ---------------------------------------------------------------------------
# State global du bot
# ---------------------------------------------------------------------------
class BotState:
    def __init__(self):
        self.access_token: Optional[str] = None
        self.md_access_token: Optional[str] = None
        self.user_id: Optional[int] = None
        self.account_id: Optional[int] = None
        self.account_spec: str = TRADOVATE_ACCOUNT_SPEC

        # PnL journalier
        self.daily_pnl: float = 0.0
        self.peak_equity: float = 50000.0
        self.session_start_equity: float = 50000.0

        # Profit total accumulé (pour consistency rule)
        self.total_profit: float = 0.0

        # Position courante
        self.current_position_qty: int = 0  # + = long, - = short
        self.current_position_symbol: Optional[str] = None

        # Flag de trading autorisé
        self.trading_halted: bool = False
        self.halt_reason: str = ""

        # Heure dernière auth
        self.last_auth_time: Optional[datetime] = None

        # Verrou pour éviter les ordres simultanés
        self.order_lock = asyncio.Lock()

    def reset_daily(self):
        self.daily_pnl = 0.0
        self.trading_halted = False
        self.halt_reason = ""
        logger.info("Reset journalier effectué.")


bot_state = BotState()


# ---------------------------------------------------------------------------
# Tradovate Client
# ---------------------------------------------------------------------------
class TradovateClient:
    """Client HTTP pour l'API REST Tradovate."""

    def __init__(self, state: BotState):
        self.state = state
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=TRADOVATE_BASE_URL,
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _auth_headers(self) -> dict:
        if not self.state.access_token:
            raise RuntimeError("Non authentifié — appeler auth() d'abord.")
        return {"Authorization": f"Bearer {self.state.access_token}"}

    @staticmethod
    def _encode_password(password: str) -> str:
        """Encode le mot de passe selon le schéma Tradovate web (split-reverse-base64)."""
        import base64
        mid = len(password) // 2
        encoded = password[:mid][::-1] + password[mid:][::-1]
        return base64.b64encode(encoded.encode()).decode()

    async def auth(self) -> bool:
        """Authentification via token pré-fourni uniquement.
        Le token est poussé par refresh_token.py toutes les 90 min
        via l'endpoint /refresh_token (qui définit bot_state.access_token)
        ou via TRADOVATE_ACCESS_TOKEN dans les variables Railway.
        La méthode username/password est désactivée (API Tradovate instable).
        """
        # 1. Utiliser le token déjà en mémoire si présent (défini par /refresh_token)
        if self.state.access_token:
            self.state.last_auth_time = datetime.now(timezone.utc)
            logger.info("Auth OK — token en mémoire réutilisé ✅")
            if not self.state.account_id:
                await self._load_account()
            return True

        # 2. Lire depuis env Railway (au démarrage ou après redéploiement)
        env_token = os.getenv("TRADOVATE_ACCESS_TOKEN", "")
        if env_token:
            self.state.access_token = env_token
            self.state.last_auth_time = datetime.now(timezone.utc)
            logger.info("Auth via TRADOVATE_ACCESS_TOKEN env ✅")
            await self._load_account()
            return True

        # 3. Pas de token disponible — attendre le prochain refresh cron (90 min)
        logger.warning("Aucun token Tradovate disponible — attente du refresh cron (90 min).")
        return False

    async def _load_account(self):
        """Charge les infos du compte principal."""
        try:
            client = await self._get_client()
            resp = await client.get("/account/list", headers=self._auth_headers())
            resp.raise_for_status()
            accounts = resp.json()

            if not accounts:
                logger.warning("Aucun compte trouvé.")
                return

            # Chercher le compte correspondant à ACCOUNT_SPEC
            for acc in accounts:
                spec = acc.get("name", "")
                if self.state.account_spec and self.state.account_spec in spec:
                    self.state.account_id = acc["id"]
                    logger.info(f"Compte sélectionné : id={acc['id']} name={spec}")
                    return

            # Fallback : premier compte
            self.state.account_id = accounts[0]["id"]
            logger.warning(
                f"ACCOUNT_SPEC '{self.state.account_spec}' non trouvé — "
                f"utilisation du premier compte id={accounts[0]['id']}"
            )
        except Exception as e:
            logger.error(f"Erreur chargement compte : {e}")

    async def get_account(self) -> Optional[dict]:
        """Retourne les informations du compte."""
        try:
            if not self.state.account_id:
                return None
            client = await self._get_client()
            resp = await client.get(
                f"/account/item?id={self.state.account_id}",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Erreur get_account : {e}")
            return None

    async def get_positions(self) -> list:
        """Retourne toutes les positions ouvertes."""
        try:
            client = await self._get_client()
            resp = await client.get("/position/list", headers=self._auth_headers())
            resp.raise_for_status()
            positions = resp.json()
            # Filtrer les positions non nulles
            return [p for p in positions if p.get("netPos", 0) != 0]
        except Exception as e:
            logger.error(f"Erreur get_positions : {e}")
            return []

    async def get_orders(self) -> list:
        """Retourne les ordres actifs."""
        try:
            client = await self._get_client()
            resp = await client.get("/order/list", headers=self._auth_headers())
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Erreur get_orders : {e}")
            return []

    async def place_order(
        self,
        action: str,  # "Buy" ou "Sell"
        symbol: str,
        qty: int,
        price: float,
        order_type: str = "Limit",
        time_in_force: str = "GTD",
        expire_minutes: int = 5,
    ) -> Optional[dict]:
        """Place un ordre sur Tradovate."""
        if not self.state.account_id:
            logger.error("place_order : account_id non défini.")
            return None

        # Calcul de l'expiration GTD
        from datetime import timedelta
        expire_time = (
            datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "accountSpec": self.state.account_spec,
            "accountId": self.state.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,
            "price": price,
            "timeInForce": time_in_force,
            "expireTime": expire_time,
            "isAutomated": True,
        }

        try:
            client = await self._get_client()
            resp = await client.post(
                "/order/placeorder", json=payload, headers=self._auth_headers()
            )
            resp.raise_for_status()
            result = resp.json()
            if "failureReason" in result and result["failureReason"] != "None":
                logger.error(f"Ordre rejeté : {result.get('failureReason')} — {result.get('failureText')}")
                return None
            logger.info(f"Ordre placé : {action} {qty} {symbol} @ {price} — id={result.get('orderId')}")
            return result
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error place_order : {e.response.status_code} — {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"Erreur place_order : {e}")
            return None

    async def place_market_order(
        self, action: str, symbol: str, qty: int
    ) -> Optional[dict]:
        """Place un ordre Market (pour le force-close)."""
        if not self.state.account_id:
            logger.error("place_market_order : account_id non défini.")
            return None

        payload = {
            "accountSpec": self.state.account_spec,
            "accountId": self.state.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "timeInForce": "FOK",
            "isAutomated": True,
        }

        try:
            client = await self._get_client()
            resp = await client.post(
                "/order/placeorder", json=payload, headers=self._auth_headers()
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"Ordre market : {action} {qty} {symbol} — id={result.get('orderId')}")
            return result
        except Exception as e:
            logger.error(f"Erreur place_market_order : {e}")
            return None

    async def cancel_order(self, order_id: int) -> bool:
        """Annule un ordre spécifique."""
        try:
            client = await self._get_client()
            resp = await client.post(
                "/order/cancelorder",
                json={"orderId": order_id},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            logger.info(f"Ordre {order_id} annulé.")
            return True
        except Exception as e:
            logger.error(f"Erreur cancel_order {order_id} : {e}")
            return False

    async def cancel_all_orders(self):
        """Annule tous les ordres actifs."""
        orders = await self.get_orders()
        active_statuses = {"Working", "PendingNew", "PartiallyFilled"}
        for order in orders:
            if order.get("ordStatus") in active_statuses:
                await self.cancel_order(order["id"])

    async def ensure_authenticated(self) -> bool:
        """Vérifie et renouvelle l'authentification si nécessaire (> 80 min).
        Le cron refresh tourne toutes les 90 min — on laisse de la marge.
        """
        if not self.state.access_token or not self.state.last_auth_time:
            return await self.auth()
        delta = (datetime.now(timezone.utc) - self.state.last_auth_time).total_seconds()
        if delta > 4800:  # 80 minutes (< 90 min cron refresh)
            logger.info("Token proche expiration — re-authentification...")
            return await self.auth()
        return True


# ---------------------------------------------------------------------------
# CME Guard
# ---------------------------------------------------------------------------
class CMEGuard:
    """Gère les restrictions horaires CME et les force-closes."""

    def __init__(self, state: BotState, client: TradovateClient):
        self.state = state
        self.client = client

    def _now_paris(self) -> datetime:
        return datetime.now(TZ_PARIS)

    def is_trading_allowed(self) -> tuple[bool, str]:
        """
        Retourne (allowed: bool, reason: str).
        Blackout :
          - Vendredi 22h45 → Dimanche 23h59 Paris
          - Chaque jour après 22h45 Paris
        """
        now = self._now_paris()
        weekday = now.weekday()  # 0=lundi, 4=vendredi, 5=samedi, 6=dimanche

        # Blackout weekend
        if weekday == 4 and (now.hour > 22 or (now.hour == 22 and now.minute >= 45)):
            return False, "Blackout weekend — vendredi après 22h45 Paris"
        if weekday == 5:
            return False, "Blackout weekend — samedi"
        if weekday == 6 and not (now.hour == 23 and now.minute == 59):
            return False, "Blackout weekend — dimanche avant 23h59 Paris"

        # Force-close journalier
        if now.hour > CME_FORCE_CLOSE_HOUR or (
            now.hour == CME_FORCE_CLOSE_HOUR and now.minute >= CME_FORCE_CLOSE_MINUTE
        ):
            return False, f"Après {CME_FORCE_CLOSE_HOUR}h{CME_FORCE_CLOSE_MINUTE:02d} Paris — CME bientôt fermé"

        return True, "OK"

    async def force_close_all(self, reason: str = "Force-close"):
        """Ferme toutes les positions ouvertes au market."""
        logger.warning(f"FORCE CLOSE ALL — raison : {reason}")

        # 1. Annuler tous les ordres en attente
        await self.client.cancel_all_orders()

        # 2. Fermer toutes les positions
        positions = await self.client.get_positions()
        if not positions:
            logger.info("Aucune position à fermer.")
            return

        for pos in positions:
            net_pos = pos.get("netPos", 0)
            symbol = pos.get("contractId", {}).get("name", CONTRACT_SYMBOL)
            if net_pos == 0:
                continue
            action = "Sell" if net_pos > 0 else "Buy"
            qty = abs(net_pos)
            logger.info(f"Force-close : {action} {qty} {symbol}")
            result = await self.client.place_market_order(action, symbol, qty)
            if result:
                logger.info(f"Force-close OK pour {symbol}")
            else:
                logger.error(f"Force-close ÉCHEC pour {symbol} — intervention manuelle requise !")

        self.state.trading_halted = True
        self.state.halt_reason = reason


# ---------------------------------------------------------------------------
# Apex Risk Manager
# ---------------------------------------------------------------------------
class ApexRiskManager:
    """Vérifie les règles de risque Apex avant chaque trade."""

    def __init__(self, state: BotState):
        self.state = state

    def check_daily_loss(self) -> tuple[bool, str]:
        """Retourne (ok, reason). Bloque si daily loss >= $2500."""
        if self.state.daily_pnl <= -APEX_MAX_DAILY_LOSS:
            return False, f"Daily loss atteinte : {self.state.daily_pnl:.2f}$ (max -{APEX_MAX_DAILY_LOSS}$)"
        return True, "OK"

    def check_trailing_drawdown(self) -> tuple[bool, str]:
        """Vérifie le trailing drawdown depuis le peak equity."""
        current_equity = self.state.peak_equity + self.state.daily_pnl
        drawdown = self.state.peak_equity - current_equity
        if drawdown >= APEX_TRAILING_DRAWDOWN:
            return False, f"Trailing drawdown atteint : -{drawdown:.2f}$ depuis peak {self.state.peak_equity:.2f}$"
        return True, "OK"

    def check_consistency_rule(self, potential_pnl: float) -> tuple[bool, str]:
        """
        Consistency rule PA : le profit d'une journée ne peut pas dépasser
        30% du profit total accumulé.
        """
        if self.state.total_profit <= 0:
            return True, "OK"  # Pas encore de profit total, pas de contrainte
        max_day = self.state.total_profit * APEX_CONSISTENCY_MAX_DAY_PCT
        if self.state.daily_pnl + potential_pnl > max_day:
            return (
                False,
                f"Consistency rule : profit journalier potentiel "
                f"{self.state.daily_pnl + potential_pnl:.2f}$ > 30% du total "
                f"{self.state.total_profit:.2f}$ (max {max_day:.2f}$)",
            )
        return True, "OK"

    def update_pnl(self, trade_pnl: float):
        """Met à jour le PnL journalier et le peak equity."""
        self.state.daily_pnl += trade_pnl
        current_equity = self.state.session_start_equity + self.state.daily_pnl
        if current_equity > self.state.peak_equity:
            self.state.peak_equity = current_equity
            logger.info(f"Nouveau peak equity : {self.state.peak_equity:.2f}$")

        if trade_pnl > 0:
            self.state.total_profit += trade_pnl

        logger.info(
            f"PnL journalier : {self.state.daily_pnl:.2f}$ | "
            f"Peak equity : {self.state.peak_equity:.2f}$ | "
            f"Profit total : {self.state.total_profit:.2f}$"
        )


# ---------------------------------------------------------------------------
# Calcul de la taille en contrats METH
# ---------------------------------------------------------------------------
def calculate_contracts(bet_usd: float, price: float = 0.0, sl: float = 0.0) -> int:
    """
    Calcule le nombre de contrats a trader.

    MNQ  : SL fixe = MNQ_SL_POINTS * $2/pt = $18/contrat (defaut)
    METH : SL dynamique = 0.1 * price * sl_pct
    """
    symbol = CONTRACT_SYMBOL.upper()

    if "MNQ" in symbol:
        # MNQ — SL fixe 9 points x $2/point = $18 par contrat
        sl_value_per_contract = MNQ_SL_POINTS * MNQ_POINT_VALUE
        max_risk = 500.0  # cap $500 de risque par trade
    else:
        # METH / ETH — SL dynamique par %
        if price <= 0 or sl <= 0:
            logger.warning("Prix ou SL invalide pour calculate_contracts METH.")
            return 1
        sl_pct = abs(price - sl) / price
        if sl_pct == 0:
            return 1
        sl_value_per_contract = 0.1 * price * sl_pct
        max_risk = 500.0

    if sl_value_per_contract <= 0:
        return 1

    contracts = max(1, int(bet_usd / sl_value_per_contract))
    max_contracts = max(1, int(max_risk / sl_value_per_contract))
    contracts = min(contracts, max_contracts)

    logger.info(
        f"Sizing [{symbol}] : bet={bet_usd:.0f}$ | "
        f"SL/ctr={sl_value_per_contract:.2f}$ | contracts={contracts}"
    )
    return contracts


# ---------------------------------------------------------------------------
# Initialisation des singletons
# ---------------------------------------------------------------------------
tradovate_client = TradovateClient(bot_state)
cme_guard = CMEGuard(bot_state, tradovate_client)
apex_risk = ApexRiskManager(bot_state)


# ---------------------------------------------------------------------------
# Background task — surveillance horaire (toutes les minutes)
# ---------------------------------------------------------------------------
async def cme_time_watcher():
    """Boucle asyncio : vérifie l'heure et force-close si nécessaire."""
    logger.info("CME Time Watcher démarré.")
    while True:
        try:
            await asyncio.sleep(60)  # check toutes les minutes
            allowed, reason = cme_guard.is_trading_allowed()

            if not allowed and not bot_state.trading_halted:
                logger.warning(f"Trading non autorisé : {reason}")
                await cme_guard.force_close_all(reason=reason)

            # Reset journalier à minuit Paris
            now_paris = datetime.now(TZ_PARIS)
            if now_paris.hour == 0 and now_paris.minute == 0:
                bot_state.reset_daily()

            # Re-authentification proactive toutes les 15 min
            await tradovate_client.ensure_authenticated()

        except asyncio.CancelledError:
            logger.info("CME Time Watcher arrêté.")
            break
        except Exception as e:
            logger.error(f"Erreur dans CME Time Watcher : {e}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Lifespan FastAPI
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup & shutdown — 5 piliers techniques actifs."""
    logger.info("=== Apex Tradovate Bot démarrage (5 piliers actifs) ===")

    # ── Pilier 3 : Vérification variables d'env ──────────────────────────────
    required_vars = ["TRADOVATE_USERNAME", "TRADOVATE_PASSWORD", "TRADOVATE_ACCOUNT_SPEC", "APEX_WEBHOOK_TOKEN"]
    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        logger.warning(f"Variables d'env manquantes : {missing}")

    # ── Pilier 1 : Restaurer l'état persisté (survit aux redémarrages Railway) ─
    saved = state_manager.load()
    if saved:
        bot_state.daily_pnl    = saved.get("daily_pnl", 0.0)
        bot_state.peak_equity  = saved.get("peak_equity", 50000.0)
        bot_state.total_profit = saved.get("total_profit", 0.0)
        logger.info(
            f"État restauré — daily_pnl={bot_state.daily_pnl:.2f}$ "
            f"peak={bot_state.peak_equity:.2f}$ total={bot_state.total_profit:.2f}$"
        )

    # Auth initiale
    if DRY_RUN:
        logger.info("=== MODE DRY_RUN ACTIVE — aucun ordre reel n'est envoye ===")
    ok = await tradovate_client.auth()
    if ok:
        logger.info("Connexion Tradovate etablie.")
        await notify("🟢 Apex NQ Bot démarré — Tradovate connecté.")  # Pilier 4
    else:
        if DRY_RUN:
            logger.warning("Auth Tradovate echouee mais DRY_RUN=true — simulation uniquement.")
        else:
            logger.error("ATTENTION : Connexion Tradovate echouee au demarrage !")
            await notify("🔴 Auth Tradovate ÉCHOUÉE au démarrage !", urgent=True)  # Pilier 4

    # Démarrer le watcher CME
    watcher_task = asyncio.create_task(cme_time_watcher())

    yield  # ── app running ──

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("=== Arrêt du bot ===")

    # ── Pilier 4 : Notifier le redémarrage ───────────────────────────────────
    await notify("🟠 Bot arrêté — redémarrage Railway probable.")

    # ── Pilier 1 : Sauvegarder l'état avant arrêt ────────────────────────────
    state_manager.save({
        "daily_pnl":      bot_state.daily_pnl,
        "peak_equity":    bot_state.peak_equity,
        "total_profit":   bot_state.total_profit,
        "trading_halted": bot_state.trading_halted,
        "halt_reason":    bot_state.halt_reason,
    })
    logger.info("État sauvegardé avant arrêt (Pilier 1).")

    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    await tradovate_client.close()


# ---------------------------------------------------------------------------
# Application FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Apex Tradovate Bot",
    description="Bot de trading Apex Trader Funding via Tradovate API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Modèles Pydantic
# ---------------------------------------------------------------------------
class TradingViewSignal(BaseModel):
    action: str          # "buy", "sell", "close"
    symbol: str          # ex: "MNQ"
    price: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tf: Optional[int] = None
    strategy: Optional[str] = None
    token: Optional[str] = None  # token optionnel dans le body (TradingView)
    close_reason: Optional[str] = None    # "TP1" | "TP2" | "SL" | "signal"
    # Champs rapport d'ordre
    atr: Optional[float] = None            # ATR au moment du signal
    hma20: Optional[float] = None          # Hull MA 20
    range_size: Optional[float] = None     # Taille du range (high - low bougie)
    avg_level: Optional[float] = None      # Moyenne des hauts (SELL) ou des bas (BUY)


# ---------------------------------------------------------------------------
# Registre des ordres (pour endpoint /orders/recent + cron notification)
# ---------------------------------------------------------------------------
# (uuid + dataclasses importés en haut du fichier)

@dataclass
class BotOrderRecord:
    id: str
    timestamp: str
    event: str            # "open" | "close"
    action: str           # "Buy" | "Sell" | "Close"
    symbol: str
    qty: int
    price: float
    bet_usd: float
    dry_run: bool
    strategy: str = ""
    # Indicateurs
    sl: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    atr: float = 0.0
    hma20: float = 0.0
    range_size: float = 0.0
    avg_level: float = 0.0
    # Fermeture
    close_reason: str = ""
    exit_price: float = 0.0
    pnl_usd: float = 0.0
    pnl_pts: float = 0.0
    notified: bool = False

# Liste en mémoire — max 200 enregistrements
_order_log: List[BotOrderRecord] = []
_order_log_lock = asyncio.Lock()

async def _log_order(record: BotOrderRecord):
    async with _order_log_lock:
        _order_log.append(record)
        if len(_order_log) > 200:
            _order_log.pop(0)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """
    Pilier 5 — Health Check.
    Railway utilise cet endpoint pour vérifier que le bot est vivant.
    Répond toujours HTTP 200 si le process tourne (même si auth KO).
    Détails complets disponibles sur /status.
    """
    return {
        "status":         "ok",
        "bot_alive":      True,
        "dry_run":        DRY_RUN,
        "authenticated":  bool(bot_state.access_token),
        "trading_halted": bot_state.trading_halted,
        "daily_pnl":      round(bot_state.daily_pnl, 2),
        "timestamp":      datetime.now(TZ_PARIS).isoformat(),
    }


@app.get("/status")
async def status():
    """État complet du bot : positions, Labouchere, PnL, horaires."""
    allowed, reason = cme_guard.is_trading_allowed()
    positions = await tradovate_client.get_positions()
    lab_state = get_state() if LAB_AVAILABLE else {"error": "module non disponible"}

    return {
        "bot": {
            "trading_allowed": allowed,
            "trading_halted": bot_state.trading_halted,
            "halt_reason": bot_state.halt_reason if bot_state.trading_halted else reason if not allowed else "",
            "authenticated": bool(bot_state.access_token),
            "account_id": bot_state.account_id,
            "account_spec": bot_state.account_spec,
            "contract_symbol": CONTRACT_SYMBOL,
        },
        "pnl": {
            "daily_pnl": round(bot_state.daily_pnl, 2),
            "total_profit": round(bot_state.total_profit, 2),
            "peak_equity": round(bot_state.peak_equity, 2),
            "session_start_equity": round(bot_state.session_start_equity, 2),
            "max_daily_loss_remaining": round(APEX_MAX_DAILY_LOSS + bot_state.daily_pnl, 2),
            "trailing_drawdown_remaining": round(
                APEX_TRAILING_DRAWDOWN - (bot_state.peak_equity - (bot_state.session_start_equity + bot_state.daily_pnl)), 2
            ),
        },
        "labouchere": lab_state,
        "positions": positions,
        "time_paris": datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


@app.post("/refresh_token")
async def refresh_token_endpoint(request: Request, x_webhook_token: Optional[str] = Header(None)):
    """Reçoit un nouveau token Tradovate depuis le script local de refresh."""
    _verify_token(x_webhook_token)
    body = await request.json()
    new_token = body.get("access_token")
    if not new_token:
        raise HTTPException(status_code=400, detail="access_token requis")
    bot_state.access_token = new_token
    bot_state.last_auth_time = datetime.now(timezone.utc)
    # Recharger le compte
    await tradovate_client._load_account()
    logger.info(f"Token rafraîchi via endpoint — account_id={bot_state.account_id}")
    return {"status": "ok", "authenticated": True, "account_id": bot_state.account_id}


@app.post("/close_all")
async def close_all_endpoint(request: Request, x_webhook_token: Optional[str] = Header(None)):
    """Force-close toutes les positions (protégé par token)."""
    _verify_token(x_webhook_token)
    await cme_guard.force_close_all(reason="Force-close manuel via API")
    return {"status": "ok", "message": "Toutes les positions fermées."}


@app.post("/webhook/apex")
@app.post("/webhook/apex/{url_token}")
async def webhook_apex(
    signal: TradingViewSignal,
    background_tasks: BackgroundTasks,
    url_token: Optional[str] = None,
    x_webhook_token: Optional[str] = Header(None),
):
    """
    Recoit un signal TradingView et execute l ordre correspondant.
    Token accepte : header X-Webhook-Token, URL path, ou champ body.
    URL TradingView : /webhook/apex/jp_apex_mnq_2026
    """
    effective_token = x_webhook_token or url_token or signal.token
    _verify_token(effective_token)

    logger.info(
        f"Signal reçu : action={signal.action} symbol={signal.symbol} "
        f"price={signal.price} sl={signal.sl} strategy={signal.strategy}"
    )

    # 1. Vérifier l'authentification
    if DRY_RUN:
        logger.info("[DRY_RUN] Signal recu — simulation (pas d'ordre reel).")
    elif not await tradovate_client.ensure_authenticated():
        raise HTTPException(status_code=503, detail="Tradovate non authentifie.")

    # 2. Vérifier les horaires CME
    if bot_state.trading_halted:
        return JSONResponse(
            status_code=403,
            content={"status": "halted", "reason": bot_state.halt_reason},
        )

    allowed, reason = cme_guard.is_trading_allowed()
    if not allowed:
        logger.warning(f"Signal refusé — trading non autorisé : {reason}")
        return JSONResponse(
            status_code=403,
            content={"status": "not_allowed", "reason": reason},
        )

    action = signal.action.lower().strip()

    # 3. Action CLOSE
    if action == "close":
        close_label = f"Signal TradingView CLOSE ({signal.close_reason or 'signal'})"
        background_tasks.add_task(
            _execute_close, reason=close_label, signal=signal
        )
        return {"status": "ok", "action": "close", "message": "Fermeture en cours.", "reason": signal.close_reason or "signal"}

    # 4. Action BUY / SELL
    if action not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"Action inconnue : {action}")

    # SL optionnel pour MNQ (SL fixe interne = MNQ_SL_POINTS x $2/point)
    if signal.sl is None and "MNQ" not in CONTRACT_SYMBOL.upper():
        raise HTTPException(
            status_code=400,
            detail="SL (stop-loss) requis pour calculer la taille de position."
        )

    # 5. Vérifications de risque Apex
    daily_ok, daily_reason = apex_risk.check_daily_loss()
    if not daily_ok:
        logger.warning(f"Ordre bloqué (daily loss) : {daily_reason}")
        bot_state.trading_halted = True
        bot_state.halt_reason = daily_reason
        return JSONResponse(
            status_code=403,
            content={"status": "risk_blocked", "reason": daily_reason},
        )

    trail_ok, trail_reason = apex_risk.check_trailing_drawdown()
    if not trail_ok:
        logger.warning(f"Ordre bloqué (trailing drawdown) : {trail_reason}")
        bot_state.trading_halted = True
        bot_state.halt_reason = trail_reason
        return JSONResponse(
            status_code=403,
            content={"status": "risk_blocked", "reason": trail_reason},
        )

    # 6. Labouchere — obtenir la mise
    bet_usd = get_current_bet() if LAB_AVAILABLE else UNIT_DOLLAR
    logger.info(f"Labouchere mise : {bet_usd}$")

    # 7. Calcul de la taille
    contracts = calculate_contracts(bet_usd, signal.price, signal.sl)

    # 8. Consistency rule (estimation : on suppose que le trade peut rapporter bet_usd)
    cons_ok, cons_reason = apex_risk.check_consistency_rule(bet_usd)
    if not cons_ok:
        logger.warning(f"Ordre bloqué (consistency rule) : {cons_reason}")
        return JSONResponse(
            status_code=403,
            content={"status": "risk_blocked", "reason": cons_reason},
        )

    # 9. Exécuter l'ordre en background
    tradovate_action = "Buy" if action == "buy" else "Sell"
    background_tasks.add_task(
        _execute_order,
        tradovate_action,
        CONTRACT_SYMBOL,
        contracts,
        signal.price,
        bet_usd,
        signal,   # rapport complet
    )

    return {
        "status": "ok",
        "action": tradovate_action,
        "symbol": CONTRACT_SYMBOL,
        "contracts": contracts,
        "price": signal.price,
        "bet_usd": bet_usd,
        "labouchere_available": LAB_AVAILABLE,
    }


# ---------------------------------------------------------------------------
# Fonctions d'exécution asynchrones (background tasks)
# ---------------------------------------------------------------------------
def _format_order_report(
    action: str, symbol: str, qty: int, fill_price: float,
    bet_usd: float, signal: "TradingViewSignal", dry_run: bool = False
) -> str:
    """Génère le rapport texte pour un ordre exécuté."""
    direction = "🟢 BUY" if action == "Buy" else "🔴 SELL"
    mode_tag  = " [DRY]" if dry_run else ""
    sep       = "─" * 32

    sl_line  = f"  SL        : {signal.sl:.2f}" if signal.sl else "  SL        : —"
    tp1_val  = signal.tp1 or signal.tp
    tp1_line = f"  TP1       : {tp1_val:.2f}" if tp1_val else "  TP1       : —"
    tp2_line = f"  TP2       : {signal.tp2:.2f}" if signal.tp2 else "  TP2       : —"
    atr_line = f"  ATR       : {signal.atr:.2f}" if signal.atr else "  ATR       : —"
    hma_line = f"  HMA 20    : {signal.hma20:.2f}" if signal.hma20 else "  HMA 20    : —"
    rng_line = f"  Range     : {signal.range_size:.2f}" if signal.range_size else "  Range     : —"
    avg_label = "Moy. bas  " if action == "Buy" else "Moy. hauts"
    avg_line  = f"  {avg_label} : {signal.avg_level:.2f}" if signal.avg_level else f"  {avg_label} : —"

    lab_state = ""
    if LAB_AVAILABLE:
        try:
            from apex_lab_tracker import get_lab_state
            st = get_lab_state()
            lab_state = (
                f"  Séquence  : {st.get('sequence', [])}\n"
                f"  Mise      : ${bet_usd:.0f} ({qty} contrats)\n"
                f"  Cycle     : #{st.get('cycle', 1)}  Trades : {st.get('total_trades', 0)}\n"
                f"  Win/Loss  : {st.get('wins', 0)}W / {st.get('losses', 0)}L\n"
            )
        except Exception:
            lab_state = f"  Mise      : ${bet_usd:.0f} ({qty} contrats)\n"
    else:
        lab_state = f"  Mise      : ${bet_usd:.0f} ({qty} contrats)\n"

    report = (
        f"\n{sep}\n"
        f"📊 RAPPORT ORDRE{mode_tag}\n"
        f"{sep}\n"
        f"  {direction}  {qty}x {symbol}\n"
        f"  Entrée    : {fill_price:.2f}\n"
        f"{sep}\n"
        f"  NIVEAUX\n"
        f"{sl_line}\n"
        f"{tp1_line}\n"
        f"{tp2_line}\n"
        f"{sep}\n"
        f"  INDICATEURS\n"
        f"{atr_line}\n"
        f"{hma_line}\n"
        f"{rng_line}\n"
        f"{avg_line}\n"
        f"{sep}\n"
        f"  LABOUCHERE\n"
        f"{lab_state}"
        f"{sep}\n"
        f"  Stratégie : {signal.strategy or '—'}\n"
        f"{sep}"
    )
    return report


async def _send_notify(report: str):
    """Envoie le rapport vers NOTIFY_WEBHOOK_URL si configuré."""
    notify_url = os.getenv("NOTIFY_WEBHOOK_URL", "")
    if not notify_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(notify_url, json={"message": report}, headers={"Content-Type": "application/json"})
    except Exception as e:
        logger.warning(f"Notification échouée : {e}")


async def _execute_order(
    action: str, symbol: str, qty: int, price: float, bet_usd: float,
    signal: "TradingViewSignal" = None,
):
    """Place un ordre Market au prix du marché (pas Limit — évite les ordres hors marché)."""
    async with bot_state.order_lock:
        try:
            if DRY_RUN:
                logger.info(f"[DRY_RUN] ORDRE SIMULE : {action} {qty}x {symbol} @ {price:.2f} (bet=${bet_usd:.0f})")
                bot_state.current_position_qty = qty if action == "Buy" else -qty
                bot_state.current_position_symbol = symbol
                bot_state.current_position_price = price
                if signal:
                    report = _format_order_report(action, symbol, qty, price, bet_usd, signal, dry_run=True)
                    logger.info(report)
                    await _send_notify(report)
                    rec = BotOrderRecord(
                        id=str(uuid.uuid4())[:8], event="open",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        action=action, symbol=symbol, qty=qty, price=price,
                        bet_usd=bet_usd, dry_run=True,
                        strategy=signal.strategy or "",
                        sl=signal.sl or 0, tp1=signal.tp1 or signal.tp or 0, tp2=signal.tp2 or 0,
                        atr=signal.atr or 0, hma20=signal.hma20 or 0,
                        range_size=signal.range_size or 0, avg_level=signal.avg_level or 0,
                    )
                    await _log_order(rec)
                return

            # Ordre MARKET — exécution immédiate au meilleur prix disponible
            result = await tradovate_client.place_market_order(
                action=action,
                symbol=symbol,
                qty=qty,
            )
            if result:
                fill_price = result.get("price", price)
                logger.info(f"✅ Ordre MARKET exécuté : {action} {qty}x {symbol} @ {fill_price} (bet=${bet_usd:.0f})")
                bot_state.current_position_qty  = qty if action == "Buy" else -qty
                bot_state.current_position_symbol = symbol
                bot_state.current_position_price  = fill_price
                if signal:
                    report = _format_order_report(action, symbol, qty, fill_price, bet_usd, signal)
                    logger.info(report)
                    await _send_notify(report)
                    rec = BotOrderRecord(
                        id=str(uuid.uuid4())[:8], event="open",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        action=action, symbol=symbol, qty=qty, price=fill_price,
                        bet_usd=bet_usd, dry_run=False,
                        strategy=signal.strategy or "",
                        sl=signal.sl or 0, tp1=signal.tp1 or signal.tp or 0, tp2=signal.tp2 or 0,
                        atr=signal.atr or 0, hma20=signal.hma20 or 0,
                        range_size=signal.range_size or 0, avg_level=signal.avg_level or 0,
                    )
                    await _log_order(rec)
            else:
                logger.error(f"❌ Ordre non exécuté : {action} {qty}x {symbol}")
        except Exception as e:
            logger.error(f"Erreur _execute_order : {e}\n{traceback.format_exc()}")


def _format_close_report(
    symbol: str, qty: int, entry: float, exit_price: float,
    pnl_usd: float, pnl_pts: float, close_reason: str,
    bet_usd: float, dry_run: bool = False
) -> str:
    """Génère le rapport texte pour une clôture."""
    mode_tag = " [DRY]" if dry_run else ""
    sep = "─" * 32
    pnl_emoji = "✅" if pnl_usd >= 0 else "❌"
    pnl_sign  = "+" if pnl_usd >= 0 else ""
    pts_sign  = "+" if pnl_pts >= 0 else ""

    reason_map = {
        "TP1": "🎯 TP1 atteint",
        "TP2": "🎯🎯 TP2 atteint",
        "SL":  "🛑 SL touché",
        "CME": "⏰ Force-close CME",
    }
    reason_label = reason_map.get(close_reason.upper() if close_reason else "", f"📤 {close_reason or 'Signal close'}")

    return (
        f"\n{sep}\n"
        f"📋 RÉSULTAT POSITION{mode_tag}\n"
        f"{sep}\n"
        f"  {reason_label}\n"
        f"  Symbole   : {symbol}  x{qty}\n"
        f"  Entrée    : {entry:.2f}\n"
        f"  Sortie    : {exit_price:.2f}\n"
        f"  Points    : {pts_sign}{pnl_pts:.2f} pts\n"
        f"  PnL       : {pnl_emoji} {pnl_sign}{pnl_usd:.2f} $\n"
        f"  Mise      : {bet_usd:.0f} $\n"
        f"{sep}"
    )


async def _execute_close(reason: str = "Close signal", signal: "TradingViewSignal" = None):
    """Ferme toutes les positions et met a jour le Labouchere."""
    async with bot_state.order_lock:
        try:
            close_reason = ""
            exit_price_tv = signal.price if signal else 0.0
            if signal:
                close_reason = (signal.close_reason or signal.strategy or "signal").upper()

            if DRY_RUN:
                qty_open = bot_state.current_position_qty
                if qty_open != 0:
                    entry   = bot_state.current_position_price or 0.0
                    exit_p  = exit_price_tv or entry
                    sym     = bot_state.current_position_symbol or CONTRACT_SYMBOL
                    # PnL estimé (direction dépend de Buy/Sell)
                    direction = 1 if qty_open > 0 else -1
                    pnl_pts = (exit_p - entry) * direction
                    pnl_usd = pnl_pts * abs(qty_open) * MNQ_POINT_VALUE

                    report = _format_close_report(
                        sym, abs(qty_open), entry, exit_p,
                        pnl_usd, pnl_pts, close_reason, UNIT_DOLLAR, dry_run=True
                    )
                    logger.info(report)
                    await _send_notify(report)

                    rec = BotOrderRecord(
                        id=str(uuid.uuid4())[:8], event="close",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        action="Close", symbol=sym, qty=abs(qty_open),
                        price=entry, bet_usd=UNIT_DOLLAR, dry_run=True,
                        strategy=signal.strategy if signal else "",
                        close_reason=close_reason,
                        exit_price=exit_p, pnl_usd=pnl_usd, pnl_pts=pnl_pts,
                    )
                    await _log_order(rec)

                    if LAB_AVAILABLE:
                        if pnl_usd >= 0:
                            record_win(pnl_usd)
                        else:
                            record_loss(abs(pnl_usd))
                else:
                    logger.info("[DRY_RUN] Pas de position ouverte a fermer.")
                bot_state.current_position_qty = 0
                bot_state.current_position_symbol = None
                return

            positions = await tradovate_client.get_positions()
            if not positions:
                logger.info("Aucune position a fermer.")
                return

            for pos in positions:
                net_pos = pos.get("netPos", 0)
                if net_pos == 0:
                    continue

                symbol = pos.get("contractId", {}).get("name", CONTRACT_SYMBOL)
                action = "Sell" if net_pos > 0 else "Buy"
                qty = abs(net_pos)

                open_pl = pos.get("openPL", 0.0)
                trade_pnl = float(open_pl) if open_pl else 0.0

                result = await tradovate_client.place_market_order(action, symbol, qty)
                if result:
                    logger.info(f"Position fermee : {symbol} pnl={trade_pnl:.2f}$")

                    # Rapport fermeture
                    entry_p = bot_state.current_position_price or pos.get("netPrice", 0)
                    exit_p  = exit_price_tv or entry_p
                    direction = 1 if net_pos > 0 else -1
                    pnl_pts = (exit_p - entry_p) * direction if exit_p else 0.0
                    report_close = _format_close_report(
                        symbol, qty, entry_p, exit_p,
                        trade_pnl, pnl_pts, close_reason, UNIT_DOLLAR
                    )
                    logger.info(report_close)
                    await _send_notify(report_close)
                    rec = BotOrderRecord(
                        id=str(uuid.uuid4())[:8], event="close",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        action="Close", symbol=symbol, qty=qty,
                        price=entry_p, bet_usd=UNIT_DOLLAR, dry_run=False,
                        strategy=signal.strategy if signal else "",
                        close_reason=close_reason,
                        exit_price=exit_p, pnl_usd=trade_pnl, pnl_pts=pnl_pts,
                    )
                    await _log_order(rec)

                    if LAB_AVAILABLE:
                        if trade_pnl >= 0:
                            record_win(trade_pnl)
                        else:
                            record_loss(abs(trade_pnl))

                    apex_risk.update_pnl(trade_pnl)
                else:
                    logger.error(f"Echec fermeture position {symbol}")

            bot_state.current_position_qty = 0
            bot_state.current_position_symbol = None

        except Exception as e:
            logger.error(f"Erreur _execute_close : {e}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Endpoint /orders/recent — consulté par le cron de notification WhatsApp
# ---------------------------------------------------------------------------
@app.get("/orders/recent")
async def orders_recent(since: Optional[str] = None, limit: int = 50):
    """
    Retourne les derniers ordres enregistrés.
    since : ISO timestamp (optionnel) — ne retourne que les ordres après cette date
    limit : nombre max d'entrées (défaut 50)
    """
    async with _order_log_lock:
        orders = list(_order_log)

    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            orders = [o for o in orders if datetime.fromisoformat(o.timestamp) > since_dt]
        except Exception:
            pass

    orders = orders[-limit:]
    return {"count": len(orders), "orders": [asdict(o) for o in orders]}


@app.post("/orders/mark-notified")
async def orders_mark_notified(ids: list[str]):
    """Marque des ordres comme notifiés (appelé par le cron après envoi WhatsApp)."""
    async with _order_log_lock:
        for o in _order_log:
            if o.id in ids:
                o.notified = True
    return {"ok": True, "marked": len(ids)}


# ---------------------------------------------------------------------------
# Helper — vérification du token webhook
# ---------------------------------------------------------------------------
def _verify_token(token: Optional[str]):
    """Lève une 401 si le token est invalide."""
    if not APEX_WEBHOOK_TOKEN:
        return  # Pas de token configuré → pas de vérif (dev mode)
    if token != APEX_WEBHOOK_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Token webhook invalide. Ajouter le header X-Webhook-Token.",
        )


# ---------------------------------------------------------------------------
# Dashboard HTML — mis à jour toutes les 10s
# ---------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    from fastapi.responses import HTMLResponse
    st = await get_status()
    bot  = st.get("bot", {})
    pnl  = st.get("pnl", {})
    lab  = st.get("labouchere", {})
    pos  = st.get("positions", [])
    t    = st.get("time_paris", "")

    # Couleurs
    auth_color  = "#00e676" if bot.get("authenticated") else "#ff5252"
    halt_color  = "#ff5252" if bot.get("trading_halted") else "#00e676"
    pnl_color   = "#00e676" if pnl.get("daily_pnl", 0) >= 0 else "#ff5252"
    cum_color   = "#00e676" if lab.get("cum_pnl", 0) >= 0 else "#ff5252"

    # Historique ordres
    orders_data = []
    try:
        from collections import deque
        orders_data = list(_order_log)[-20:]
    except Exception:
        pass

    rows = ""
    for o in reversed(orders_data):
        pnl_val = getattr(o, "pnl_usd", None)
        pnl_str = f"${pnl_val:+.0f}" if pnl_val is not None else "—"
        pnl_c = "#00e676" if (pnl_val or 0) > 0 else ("#ff5252" if (pnl_val or 0) < 0 else "#aaa")
        side_icon = "▲" if getattr(o, "side", "") == "buy" else "▼"
        side_c = "#00e676" if getattr(o, "side", "") == "buy" else "#ff5252"
        status_str = getattr(o, "status", "")
        ts = str(getattr(o, "timestamp", ""))[:16].replace("T", " ")
        rows += f"""
        <tr>
          <td style='color:#aaa'>{ts}</td>
          <td style='color:{side_c}'>{side_icon} {getattr(o,'side','').upper()}</td>
          <td>{getattr(o,'qty','')}x {getattr(o,'symbol','')}</td>
          <td>${getattr(o,'price',0):.0f}</td>
          <td style='color:{"#00e676" if status_str=="filled" else "#ff9800"}'>{status_str}</td>
          <td style='color:{pnl_c};font-weight:bold'>{pnl_str}</td>
        </tr>"""

    seq_str = " + ".join(str(x) for x in lab.get("sequence", []))
    bet = lab.get("risk_trade_usd", 0)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta http-equiv="refresh" content="10"/>
  <title>Apex NQ Bot — Dashboard</title>
  <style>
    * {{ box-sizing:border-box; margin:0; padding:0 }}
    body {{ background:#0d1117; color:#e6edf3; font-family:'Segoe UI',sans-serif; padding:16px }}
    h1 {{ color:#58a6ff; margin-bottom:16px; font-size:1.3rem }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:16px }}
    .card {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px }}
    .card .label {{ color:#8b949e; font-size:.75rem; margin-bottom:4px }}
    .card .value {{ font-size:1.25rem; font-weight:700 }}
    .section {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px; margin-bottom:16px }}
    .section h2 {{ color:#58a6ff; font-size:.9rem; margin-bottom:12px }}
    table {{ width:100%; border-collapse:collapse; font-size:.82rem }}
    th {{ color:#8b949e; text-align:left; padding:4px 8px; border-bottom:1px solid #30363d }}
    td {{ padding:5px 8px; border-bottom:1px solid #21262d }}
    .seq {{ background:#21262d; border-radius:4px; padding:6px 10px; font-family:monospace; color:#79c0ff; font-size:1rem }}
    .footer {{ color:#8b949e; font-size:.72rem; margin-top:8px }}
  </style>
</head>
<body>
  <h1>🤖 Apex NQ Bot — Live Dashboard</h1>

  <div class="grid">
    <div class="card">
      <div class="label">Authentifié</div>
      <div class="value" style="color:{auth_color}">{"✅ OUI" if bot.get("authenticated") else "❌ NON"}</div>
    </div>
    <div class="card">
      <div class="label">Trading</div>
      <div class="value" style="color:{halt_color}">{"⛔ HALT" if bot.get("trading_halted") else "✅ ACTIF"}</div>
    </div>
    <div class="card">
      <div class="label">P&L Jour</div>
      <div class="value" style="color:{pnl_color}">${pnl.get("daily_pnl",0):+.0f}</div>
    </div>
    <div class="card">
      <div class="label">P&L Cumulé</div>
      <div class="value" style="color:{cum_color}">${lab.get("cum_pnl",0):+.0f}</div>
    </div>
    <div class="card">
      <div class="label">Drawdown restant</div>
      <div class="value" style="color:#ff9800">${pnl.get("trailing_drawdown_remaining",0):.0f}</div>
    </div>
    <div class="card">
      <div class="label">Limite jour restante</div>
      <div class="value" style="color:#ff9800">${pnl.get("max_daily_loss_remaining",0):.0f}</div>
    </div>
  </div>

  <div class="section">
    <h2>📊 Labouchere</h2>
    <div style="margin-bottom:10px">
      <span class="label" style="color:#8b949e;font-size:.75rem">Séquence active : </span>
      <span class="seq">[{seq_str}]</span>
      <span style="color:#79c0ff;margin-left:12px;font-weight:700">Mise = ${bet:.0f}</span>
    </div>
    <div class="grid" style="margin-bottom:0">
      <div class="card">
        <div class="label">Wins / Losses</div>
        <div class="value" style="color:#e6edf3">{lab.get("wins",0)}W / {lab.get("losses",0)}L</div>
      </div>
      <div class="card">
        <div class="label">Win Rate</div>
        <div class="value" style="color:{'#00e676' if lab.get('win_rate',0)>=50 else '#ff9800'}">{lab.get("win_rate",0):.0f}%</div>
      </div>
      <div class="card">
        <div class="label">Cycles</div>
        <div class="value">{lab.get("cycles",0)}</div>
      </div>
      <div class="card">
        <div class="label">Compte</div>
        <div class="value" style="color:#8b949e;font-size:.9rem">{bot.get("account_spec","—")}</div>
      </div>
    </div>
  </div>

  {"<div class='section'><h2>📈 Position ouverte</h2><p style='color:#ff9800'>"+str(pos[0])+"</p></div>" if pos else ""}

  <div class="section">
    <h2>📋 Historique ordres récents</h2>
    {"<p style='color:#8b949e;font-size:.82rem'>Aucun ordre enregistré</p>" if not rows else f"<table><thead><tr><th>Heure</th><th>Sens</th><th>Qté</th><th>Prix</th><th>Statut</th><th>P&L</th></tr></thead><tbody>{rows}</tbody></table>"}
  </div>

  <div class="footer">⟳ Actualisation auto toutes les 10s — {t}</div>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Exception handlers globaux
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Erreur non gérée : {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"error": "Erreur interne du serveur", "detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# Entrypoint direct (optionnel)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "apex_tradovate_bot:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
