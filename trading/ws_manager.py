"""
ws_manager.py — Pilier 2 : WebSocket avec reconnexion automatique
==================================================================
Gère la connexion WebSocket au flux de market data Tradovate.
Reconnexion instantanée en cas de coupure. Gestion des erreurs 429 + token expiré.

Variables d'environnement :
    TRADOVATE_WS_URL : URL WebSocket Tradovate
                       demo  → wss://demo.tradovateapi.com/v1/websocket
                       live  → wss://live.tradovateapi.com/v1/websocket

Usage :
    from ws_manager import TradovateWSManager

    async def on_tick(price: float):
        bar = builder.on_tick(price)
        if bar: process_bar(bar)

    ws = TradovateWSManager(token_getter=lambda: bot_state.access_token)
    ws.subscribe_ticks("NQ", on_tick)
    await ws.run_forever()   # boucle de reconnexion automatique
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

logger = logging.getLogger("ws_manager")

_TRADOVATE_ENV = os.getenv("TRADOVATE_ENV", "demo")
_WS_URLS = {
    "demo": "wss://demo.tradovateapi.com/v1/websocket",
    "live": "wss://live.tradovateapi.com/v1/websocket",
}
_WS_URL_DEFAULT = _WS_URLS.get(_TRADOVATE_ENV, _WS_URLS["demo"])
TRADOVATE_WS_URL = os.getenv("TRADOVATE_WS_URL", _WS_URL_DEFAULT)

# Délais de reconnexion (secondes) — exponentiel plafonné à 60s
_RECONNECT_DELAYS = [1, 2, 5, 10, 30, 60]


class TradovateWSManager:
    """
    Gère la connexion WebSocket Tradovate avec :
    - Reconnexion automatique sur déconnexion
    - Re-auth si token expiré (code 401)
    - Backoff exponentiel sur erreurs 429
    - Heartbeat pour détecter les connexions fantômes
    """

    def __init__(
        self,
        token_getter:  Callable[[], Optional[str]],
        reauth_cb:     Optional[Callable[[], Coroutine]] = None,
        heartbeat_sec: int = 30,
    ):
        """
        token_getter  : fonction synchrone qui retourne le token actuel
        reauth_cb     : coroutine appelée si le token expire (doit retourner True si succès)
        heartbeat_sec : intervalle du heartbeat WebSocket (secondes)
        """
        self.token_getter   = token_getter
        self.reauth_cb      = reauth_cb
        self.heartbeat_sec  = heartbeat_sec
        self._symbol:        Optional[str] = None
        self._tick_callback: Optional[Callable] = None
        self._running        = False
        self._reconnect_idx  = 0
        self._last_msg_time: Optional[datetime] = None

    def subscribe_ticks(self, symbol: str, callback: Callable) -> None:
        """Enregistre le symbol à suivre et le callback qui recevra chaque prix."""
        self._symbol        = symbol
        self._tick_callback = callback
        logger.info(f"WS : abonnement tick → {symbol}")

    async def run_forever(self) -> None:
        """Boucle principale : se reconnecte automatiquement jusqu'à l'arrêt."""
        self._running = True
        logger.info(f"TradovateWSManager démarré — {TRADOVATE_WS_URL}")

        while self._running:
            try:
                await self._connect_and_listen()
                # Sortie propre sans exception → arrêt volontaire
                if not self._running:
                    break
                logger.warning("Connexion WS terminée proprement — reconnexion...")
            except asyncio.CancelledError:
                logger.info("WS manager annulé.")
                break
            except Exception as e:
                logger.error(f"Erreur WS : {e}")

            # Délai avant reconnexion
            delay = _RECONNECT_DELAYS[min(self._reconnect_idx, len(_RECONNECT_DELAYS) - 1)]
            self._reconnect_idx += 1
            logger.info(f"Reconnexion dans {delay}s (tentative #{self._reconnect_idx})...")
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        """Arrêt propre du manager."""
        self._running = False
        logger.info("TradovateWSManager arrêté.")

    async def _connect_and_listen(self) -> None:
        """Connexion WebSocket et écoute des messages."""
        try:
            import websockets  # pip install websockets
        except ImportError:
            logger.error("websockets non installé — pip install websockets")
            await asyncio.sleep(30)
            return

        token = self.token_getter()
        if not token:
            logger.warning("Pas de token d'accès — attente 10s avant reconnexion.")
            await asyncio.sleep(10)
            return

        headers = {"Authorization": f"Bearer {token}"}
        logger.info(f"Connexion WebSocket → {TRADOVATE_WS_URL}")

        try:
            async with websockets.connect(
                TRADOVATE_WS_URL,
                additional_headers=headers,
                ping_interval=self.heartbeat_sec,
                ping_timeout=20,
                close_timeout=10,
            ) as ws:
                self._reconnect_idx = 0   # Reset backoff sur connexion réussie
                self._last_msg_time = datetime.now(timezone.utc)
                logger.info("WebSocket connecté ✅")

                # Abonnement au flux de ticks
                if self._symbol:
                    await self._subscribe(ws, self._symbol)

                # Boucle de lecture
                async for raw_msg in ws:
                    await self._handle_message(raw_msg)

        except Exception as e:
            # Classifier l'erreur pour adapter la réponse
            err_str = str(e).lower()

            if "401" in err_str or "unauthorized" in err_str or "auth" in err_str:
                logger.warning("Token expiré — tentative de re-auth.")
                if self.reauth_cb:
                    success = await self.reauth_cb()
                    if not success:
                        logger.error("Re-auth échouée — attente 30s.")
                        await asyncio.sleep(30)

            elif "429" in err_str or "too many" in err_str:
                logger.warning("Rate limit (429) — attente 60s.")
                await asyncio.sleep(60)

            else:
                raise  # Remonte pour la boucle run_forever

    async def _subscribe(self, ws, symbol: str) -> None:
        """Envoie la commande d'abonnement au flux de ticks."""
        sub_msg = json.dumps({
            "type":   "subscribe",
            "symbol": symbol,
            "data":   "Tick",
        })
        await ws.send(sub_msg)
        logger.info(f"Abonnement tick envoyé pour {symbol}")

    async def _handle_message(self, raw: str) -> None:
        """Traite un message WebSocket entrant."""
        self._last_msg_time = datetime.now(timezone.utc)

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("e") or msg.get("type") or msg.get("eventType", "")

        # Tick de prix
        if msg_type in ("md", "tick", "quote"):
            price = self._extract_price(msg)
            if price and self._tick_callback:
                if asyncio.iscoroutinefunction(self._tick_callback):
                    await self._tick_callback(price)
                else:
                    self._tick_callback(price)

        # Heartbeat / pong Tradovate
        elif msg_type == "heartbeat":
            logger.debug("WS heartbeat reçu.")

        # Erreur serveur
        elif msg_type == "error":
            logger.error(f"Erreur WS serveur : {msg}")

    @staticmethod
    def _extract_price(msg: dict) -> Optional[float]:
        """Extrait le dernier prix d'un message Tradovate."""
        # Différents formats selon l'endpoint Tradovate
        for key in ("lastPrice", "price", "lp", "tradPrice", "p"):
            val = msg.get(key) or (msg.get("data", {}) or {}).get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return None
