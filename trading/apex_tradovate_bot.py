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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from zoneinfo import ZoneInfo

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

TRADOVATE_BASE_URL = os.getenv("TRADOVATE_BASE_URL", "https://demo-api.tradovate.com/v1")
TRADOVATE_USERNAME = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD = os.getenv("TRADOVATE_PASSWORD", "")
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

    async def auth(self) -> bool:
        """Authentification et récupération du token."""
        if not TRADOVATE_USERNAME or not TRADOVATE_PASSWORD:
            logger.error("TRADOVATE_USERNAME / TRADOVATE_PASSWORD non définis.")
            return False

        payload = {
            "name": TRADOVATE_USERNAME,
            "password": TRADOVATE_PASSWORD,
            "appId": "Sample App",
            "appVersion": "1.0",
            "cid": 0,
            "sec": "",
        }
        try:
            client = await self._get_client()
            resp = await client.post("/auth/accesstokenrequest", json=payload)
            resp.raise_for_status()
            data = resp.json()

            if "errorText" in data:
                logger.error(f"Auth Tradovate échouée : {data['errorText']}")
                return False

            self.state.access_token = data.get("accessToken")
            self.state.md_access_token = data.get("mdAccessToken")
            self.state.user_id = data.get("userId")
            self.state.last_auth_time = datetime.now(timezone.utc)
            logger.info(f"Auth Tradovate OK — userId={self.state.user_id}")

            # Récupérer le compte
            await self._load_account()
            return True

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error auth : {e.response.status_code} — {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Erreur auth : {e}")
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
        """Vérifie et renouvelle l'authentification si nécessaire (> 20 min)."""
        if not self.state.access_token or not self.state.last_auth_time:
            return await self.auth()
        delta = (datetime.now(timezone.utc) - self.state.last_auth_time).total_seconds()
        if delta > 1200:  # 20 minutes
            logger.info("Token expiré — re-authentification...")
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
    """Startup & shutdown."""
    logger.info("=== Apex Tradovate Bot démarrage ===")

    # Vérifications de config
    missing = []
    for var in ["TRADOVATE_USERNAME", "TRADOVATE_PASSWORD", "TRADOVATE_ACCOUNT_SPEC", "APEX_WEBHOOK_TOKEN"]:
        if not os.getenv(var):
            missing.append(var)
    if missing:
        logger.warning(f"Variables d'env manquantes : {missing}")

    # Auth initiale
    if DRY_RUN:
        logger.info("=== MODE DRY_RUN ACTIVE — aucun ordre reel n'est envoye ===")
    ok = await tradovate_client.auth()
    if ok:
        logger.info("Connexion Tradovate etablie.")
    else:
        if DRY_RUN:
            logger.warning("Auth Tradovate echouee mais DRY_RUN=true — simulation uniquement.")
        else:
            logger.error("ATTENTION : Connexion Tradovate echouee au demarrage !")

    # Démarrer le watcher CME
    watcher_task = asyncio.create_task(cme_time_watcher())

    yield  # app running

    # Shutdown
    logger.info("=== Arrêt du bot ===")
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
    tf: Optional[int] = None
    strategy: Optional[str] = None
    token: Optional[str] = None  # token optionnel dans le body (TradingView)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Healthcheck basique."""
    return {"status": "ok", "timestamp": datetime.now(TZ_PARIS).isoformat()}


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
        background_tasks.add_task(
            _execute_close, reason="Signal TradingView CLOSE"
        )
        return {"status": "ok", "action": "close", "message": "Fermeture en cours."}

    # 4. Action BUY / SELL
    if action not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"Action inconnue : {action}")

    if signal.sl is None:
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
async def _execute_order(
    action: str, symbol: str, qty: int, price: float, bet_usd: float
):
    """Place un ordre Limit et gere le resultat Labouchere."""
    async with bot_state.order_lock:
        try:
            if DRY_RUN:
                logger.info(f"[DRY_RUN] ORDRE SIMULE : {action} {qty}x {symbol} @ {price:.2f} (bet=${bet_usd:.0f})")
                bot_state.current_position_qty = qty if action == "Buy" else -qty
                bot_state.current_position_symbol = symbol
                bot_state.current_position_price = price
                return

            result = await tradovate_client.place_order(
                action=action,
                symbol=symbol,
                qty=qty,
                price=price,
            )
            if result:
                logger.info(f"Ordre execute : {action} {qty} {symbol} @ {price}")
                bot_state.current_position_qty = qty if action == "Buy" else -qty
                bot_state.current_position_symbol = symbol
            else:
                logger.error("Ordre non execute.")
        except Exception as e:
            logger.error(f"Erreur _execute_order : {e}\n{traceback.format_exc()}")


async def _execute_close(reason: str = "Close signal"):
    """Ferme toutes les positions et met a jour le Labouchere."""
    async with bot_state.order_lock:
        try:
            if DRY_RUN:
                if bot_state.current_position_qty != 0:
                    logger.info(f"[DRY_RUN] CLOTURE SIMULEE : {bot_state.current_position_symbol} qty={bot_state.current_position_qty} raison={reason}")
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
