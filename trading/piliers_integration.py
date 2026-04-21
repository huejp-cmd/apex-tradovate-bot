"""
piliers_integration.py — Patch d'intégration des 5 piliers dans apex_tradovate_bot.py
=======================================================================================
Ce fichier montre exactement quels blocs de code ajouter/remplacer dans le bot principal.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PILIER 3 — Variables d'environnement à ajouter dans Railway (onglet Variables)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  TRADOVATE_USERNAME        sumiko
  TRADOVATE_PASSWORD        [Trousseau]
  TRADOVATE_ACCOUNT_SPEC    APEX-548673-xx
  APEX_WEBHOOK_TOKEN        [Trousseau]
  TRADOVATE_ENV             demo          ← "live" quand compte financé
  DRY_RUN                   false
  CONTRACT_SYMBOL           MNQ
  UNIT_DOLLAR               50
  TELEGRAM_TOKEN            [obtenir via @BotFather]
  TELEGRAM_CHAT_ID          [ton chat_id Telegram]
  REDIS_URL                 [optionnel — service Redis Railway]
  BOT_NAME                  Apex NQ Bot
  NOTIFY_ENABLED            true

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PILIER 1+2+4+5 — Blocs à ajouter/modifier dans apex_tradovate_bot.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ────────────────────────────────────────────────────────────────────────────
# 1. AJOUTER ces imports en haut de apex_tradovate_bot.py (après les imports existants)
# ────────────────────────────────────────────────────────────────────────────

NEW_IMPORTS = """
# ── Piliers techniques ──────────────────────────────────────────────────────
from state_manager    import state_manager                        # Pilier 1
from notifier         import notify, notify_trade_open, notify_trade_close, notify_halt, notify_low_volatility  # Pilier 4
from atr_range_builder import ATRRangeSelector, RangeBarBuilder   # ATR
from ws_manager       import TradovateWSManager                   # Pilier 2
"""

# ────────────────────────────────────────────────────────────────────────────
# 2. REMPLACER le lifespan existant par cette version enrichie
# ────────────────────────────────────────────────────────────────────────────

NEW_LIFESPAN = '''
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup & shutdown — avec les 5 piliers."""
    logger.info("=== Apex Tradovate Bot démarrage (5 piliers actifs) ===")

    # ── Pilier 3 : Vérification variables d'env ──────────────────────────────
    required_vars = [
        "TRADOVATE_USERNAME", "TRADOVATE_PASSWORD",
        "TRADOVATE_ACCOUNT_SPEC", "APEX_WEBHOOK_TOKEN"
    ]
    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        logger.warning(f"Variables d'env manquantes : {missing}")

    # ── Pilier 1 : Charger l'état persisté ──────────────────────────────────
    saved_state = state_manager.load()
    if saved_state:
        bot_state.daily_pnl    = saved_state.get("daily_pnl", 0.0)
        bot_state.peak_equity  = saved_state.get("peak_equity", 50000.0)
        bot_state.total_profit = saved_state.get("total_profit", 0.0)
        logger.info(
            f"État restauré — daily_pnl={bot_state.daily_pnl:.2f}$ "
            f"peak={bot_state.peak_equity:.2f}$ total={bot_state.total_profit:.2f}$"
        )

    # Auth initiale
    if DRY_RUN:
        logger.info("=== MODE DRY_RUN ACTIVE ===")
    ok = await tradovate_client.auth()
    if ok:
        logger.info("Connexion Tradovate établie.")
        await notify("🟢 Bot démarré — Tradovate connecté.")  # Pilier 4
    else:
        msg = "Auth Tradovate échouée au démarrage"
        if DRY_RUN:
            logger.warning(f"{msg} — DRY_RUN actif, simulation uniquement.")
        else:
            logger.error(f"ATTENTION : {msg} !")
            await notify(f"🔴 {msg}", urgent=True)           # Pilier 4

    # ── Pilier 2 : Démarrer le WebSocket market data ─────────────────────────
    atr_selector = ATRRangeSelector(http_client=None)  # TODO: passer tradovate_client._client
    ws_manager = TradovateWSManager(
        token_getter = lambda: bot_state.access_token,
        reauth_cb    = tradovate_client.auth,
    )
    # ws_manager.subscribe_ticks("NQ", on_tick_handler)  # activer quand on_tick défini
    # ws_task = asyncio.create_task(ws_manager.run_forever())

    # Démarrer le watcher CME
    watcher_task = asyncio.create_task(cme_time_watcher())

    yield  # ── app running ──

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("=== Arrêt du bot ===")
    await notify("🟠 Bot arrêté (redémarrage Railway probable).")  # Pilier 4

    # ── Pilier 1 : Sauvegarder l'état avant arrêt ────────────────────────────
    state_manager.save({
        "daily_pnl":    bot_state.daily_pnl,
        "peak_equity":  bot_state.peak_equity,
        "total_profit": bot_state.total_profit,
        "trading_halted": bot_state.trading_halted,
        "halt_reason":  bot_state.halt_reason,
    })
    logger.info("État sauvegardé avant arrêt.")

    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    await tradovate_client.close()
'''

# ────────────────────────────────────────────────────────────────────────────
# 3. REMPLACER le endpoint /health par cette version enrichie (Pilier 5)
# ────────────────────────────────────────────────────────────────────────────

NEW_HEALTH = '''
@app.get("/health")
async def health():
    """
    Pilier 5 — Health Check.
    Railway utilise cet endpoint pour savoir si le bot est vivant.
    Répond toujours 200 OK si le process tourne. Les détails sont dans /status.
    """
    return {
        "status":      "ok",
        "bot_alive":   True,
        "dry_run":     DRY_RUN,
        "authenticated": bool(bot_state.access_token),
        "trading_halted": bot_state.trading_halted,
        "daily_pnl":   bot_state.daily_pnl,
        "timestamp":   datetime.now(TZ_PARIS).isoformat(),
        "uptime_note": "Réponse sur /health = bot vivant. Voir /status pour détails complets.",
    }
'''

# ────────────────────────────────────────────────────────────────────────────
# 4. MODIFIER on_trade_result (ou la fonction qui enregistre les résultats)
#    pour persister l'état ET envoyer une notification
# ────────────────────────────────────────────────────────────────────────────

TRADE_RESULT_PATCH = '''
# Ajouter à la fin de la fonction qui traite un trade fermé :

async def _on_trade_closed(pnl: float, reason: str, symbol: str):
    """Appelé après fermeture d'un trade — Piliers 1 et 4."""
    # Mise à jour du state en mémoire (déjà fait dans le code existant)
    bot_state.daily_pnl    += pnl
    bot_state.total_profit += pnl
    bot_state.peak_equity   = max(bot_state.peak_equity, 50000.0 + bot_state.total_profit)

    # Pilier 4 : Notification téléphone
    await notify_trade_close(
        symbol    = symbol,
        pnl       = pnl,
        reason    = reason,
        daily_pnl = bot_state.daily_pnl,
    )

    # Pilier 1 : Persistance
    state_manager.save({
        "daily_pnl":    bot_state.daily_pnl,
        "peak_equity":  bot_state.peak_equity,
        "total_profit": bot_state.total_profit,
    })

    # Pilier 4 : Alerte si trading suspendu
    if bot_state.trading_halted:
        await notify_halt(bot_state.halt_reason)
'''

# ────────────────────────────────────────────────────────────────────────────
# 5. AJOUTER dans requirements.txt
# ────────────────────────────────────────────────────────────────────────────

NEW_REQUIREMENTS = """
websockets>=12.0
redis>=5.0.0
"""

if __name__ == "__main__":
    print("Ce fichier est une référence d'intégration — ne pas exécuter directement.")
    print("Ajouter les blocs ci-dessus dans apex_tradovate_bot.py selon les commentaires.")
