"""
notifier.py — Pilier 4 : Alertes & Logging
============================================
Envoie des notifications sur ton téléphone dès qu'un trade ouvre/ferme,
ou si une condition critique est atteinte (drawdown, volatilité insuffisante, etc.).

Backends supportés (par ordre de priorité) :
  1. Telegram (le plus simple à configurer sur Railway)
  2. Webhook générique (compatible OpenClaw ou n'importe quel endpoint HTTP)
  3. Fallback → simple log

Variables d'environnement :
    TELEGRAM_TOKEN      : Token du bot Telegram (ex: 110201543:AAHdqTcvCH...)
    TELEGRAM_CHAT_ID    : Chat ID (ex: 123456789)
    NOTIFY_WEBHOOK_URL  : URL webhook générique (optionnel)
    NOTIFY_ENABLED      : "true" / "false" (default: true)

Obtenir un bot Telegram en 2 min :
    1. Ouvre @BotFather sur Telegram
    2. /newbot → copie le token
    3. Envoie un message à ton bot
    4. GET https://api.telegram.org/bot<TOKEN>/getUpdates → récupère le chat_id

Usage :
    from notifier import notify
    await notify("🚀 LONG MNQ ouvert @ 21340 | SL: 21331 | TP: 21358")
    await notify("❌ DRAWDOWN LIMITE ATTEINT — trading arrêté", urgent=True)
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("notifier")

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
NOTIFY_WEBHOOK    = os.getenv("NOTIFY_WEBHOOK_URL", "")
NOTIFY_ENABLED    = os.getenv("NOTIFY_ENABLED", "true").lower() in ("1", "true", "yes")

# Préfixe bot (pour identifier la source dans Telegram)
BOT_NAME = os.getenv("BOT_NAME", "Apex NQ Bot")


# ──────────────────────────────────────────────────────────────────────────────
#  Envoi Telegram
# ──────────────────────────────────────────────────────────────────────────────

async def _send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text":    text,
                "parse_mode": "HTML",
            })
            if resp.status_code == 200:
                logger.debug("Notification Telegram envoyée.")
                return True
            else:
                logger.warning(f"Telegram erreur {resp.status_code}: {resp.text[:200]}")
                return False
    except Exception as e:
        logger.error(f"Telegram exception: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
#  Envoi Webhook générique
# ──────────────────────────────────────────────────────────────────────────────

async def _send_webhook(text: str) -> bool:
    if not NOTIFY_WEBHOOK:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(NOTIFY_WEBHOOK, json={"text": text})
            return resp.status_code < 300
    except Exception as e:
        logger.error(f"Webhook exception: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
#  Fonction principale
# ──────────────────────────────────────────────────────────────────────────────

async def notify(message: str, urgent: bool = False) -> None:
    """
    Envoie une notification sur téléphone.
    urgent=True ajoute un préfixe ⚠️ et est toujours envoyé même si NOTIFY_ENABLED=false.
    """
    ts  = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    prefix = "⚠️ URGENT" if urgent else "📊"
    full_text = f"{prefix} <b>{BOT_NAME}</b>\n{message}\n<i>{ts}</i>"

    # Log systématique (boîte noire Railway)
    log_level = logging.WARNING if urgent else logging.INFO
    logger.log(log_level, f"[NOTIFY] {message}")

    if not NOTIFY_ENABLED and not urgent:
        return

    # Tentative Telegram d'abord
    if await _send_telegram(full_text):
        return

    # Fallback webhook
    if await _send_webhook(full_text):
        return

    # Si rien n'est configuré : simple log (déjà fait ci-dessus)
    if not TELEGRAM_TOKEN and not NOTIFY_WEBHOOK:
        logger.debug("Aucun backend de notification configuré (TELEGRAM_TOKEN / NOTIFY_WEBHOOK_URL manquants).")


# ──────────────────────────────────────────────────────────────────────────────
#  Helpers thématiques
# ──────────────────────────────────────────────────────────────────────────────

async def notify_trade_open(direction: str, symbol: str, entry: float,
                             sl: float, tp: float, contracts: int,
                             lab_bet: float) -> None:
    arrow = "🚀" if direction.upper() == "LONG" else "🔻"
    await notify(
        f"{arrow} TRADE OUVERT\n"
        f"  {direction} {contracts}× {symbol} @ {entry:.2f}\n"
        f"  SL: {sl:.2f}  |  TP: {tp:.2f}\n"
        f"  Mise Labouchere: ${lab_bet:.0f}"
    )


async def notify_trade_close(symbol: str, pnl: float, reason: str,
                              daily_pnl: float) -> None:
    emoji = "✅" if pnl >= 0 else "❌"
    await notify(
        f"{emoji} TRADE CLÔTURÉ — {symbol}\n"
        f"  PnL: {pnl:+.2f}$  |  Raison: {reason}\n"
        f"  PnL journalier: {daily_pnl:+.2f}$"
    )


async def notify_halt(reason: str) -> None:
    await notify(f"🛑 BOT SUSPENDU\n  Raison: {reason}", urgent=True)


async def notify_low_volatility(tf_max: int) -> None:
    await notify(
        f"😴 VOLATILITÉ INSUFFISANTE\n"
        f"  ATR insuffisant jusqu'à {tf_max}M — trading en pause.\n"
        f"  Le bot reprendra au prochain cycle ATR."
    )


async def notify_auth_refresh(success: bool) -> None:
    if success:
        await notify("🔑 Token Tradovate rafraîchi avec succès.")
    else:
        await notify("🔑 ÉCHEC refresh token Tradovate — vérifier les credentials.", urgent=True)
