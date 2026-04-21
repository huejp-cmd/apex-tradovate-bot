"""
state_manager.py — Pilier 1 : Persistance de l'état du bot
===========================================================
Sauvegarde l'état Labouchere + position + flags dans un fichier JSON.
Support optionnel Redis si REDIS_URL est défini (recommandé sur Railway).

Variables d'environnement :
    STATE_FILE   : chemin du fichier JSON (default: /app/data/bot_state.json)
    REDIS_URL    : URL Redis si disponible (ex: redis://default:xxx@host:6379)

Usage :
    from state_manager import StateManager
    sm = StateManager()
    sm.save({"labouchere_seq": [1,2,3], "daily_pnl": -200})
    data = sm.load()
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("state_manager")

# Chemin du fichier JSON (Railway : utiliser /app/data/ ou /tmp/ si pas de volume)
_DEFAULT_FILE = os.getenv("STATE_FILE", "/tmp/bot_state.json")
_REDIS_URL    = os.getenv("REDIS_URL", "")
_REDIS_KEY    = "apex_bot_state"


# ──────────────────────────────────────────────────────────────────────────────
#  Backend Redis (optionnel)
# ──────────────────────────────────────────────────────────────────────────────

def _get_redis():
    """Retourne un client Redis synchrone, ou None si indisponible."""
    if not _REDIS_URL:
        return None
    try:
        import redis as redis_lib  # pip install redis
        r = redis_lib.from_url(_REDIS_URL, decode_responses=True, socket_timeout=5)
        r.ping()
        return r
    except Exception as e:
        logger.warning(f"Redis indisponible ({e}) — fallback JSON.")
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  StateManager
# ──────────────────────────────────────────────────────────────────────────────

class StateManager:
    """
    Gère la persistance de l'état du bot.
    Priorité : Redis > JSON file.
    """

    def __init__(self, filepath: str = _DEFAULT_FILE):
        self.filepath = Path(filepath)
        self._redis = _get_redis()

        if self._redis:
            logger.info("StateManager : backend Redis actif.")
        else:
            logger.info(f"StateManager : backend JSON → {self.filepath}")
            self.filepath.parent.mkdir(parents=True, exist_ok=True)

    # ── Sauvegarde ──────────────────────────────────────────────────────────

    def save(self, state: Dict[str, Any]) -> None:
        """Sauvegarde l'état complet (écrase l'existant)."""
        state["_saved_at"] = datetime.now(timezone.utc).isoformat()
        try:
            if self._redis:
                self._redis.set(_REDIS_KEY, json.dumps(state))
                logger.debug("State sauvegardé → Redis")
            else:
                self.filepath.write_text(json.dumps(state, indent=2))
                logger.debug(f"State sauvegardé → {self.filepath}")
        except Exception as e:
            logger.error(f"Échec sauvegarde state : {e}")

    # ── Chargement ──────────────────────────────────────────────────────────

    def load(self) -> Dict[str, Any]:
        """Charge l'état sauvegardé. Retourne {} si aucun état trouvé."""
        try:
            if self._redis:
                raw = self._redis.get(_REDIS_KEY)
                if raw:
                    data = json.loads(raw)
                    logger.info(f"State chargé depuis Redis (sauvegardé le {data.get('_saved_at','?')})")
                    return data
            else:
                if self.filepath.exists():
                    data = json.loads(self.filepath.read_text())
                    logger.info(f"State chargé depuis {self.filepath} (sauvegardé le {data.get('_saved_at','?')})")
                    return data
        except Exception as e:
            logger.error(f"Échec chargement state : {e}")
        logger.info("Aucun état persisté trouvé — démarrage à zéro.")
        return {}

    # ── Mise à jour partielle ───────────────────────────────────────────────

    def update(self, **kwargs) -> None:
        """Met à jour des clés spécifiques sans écraser tout l'état."""
        state = self.load()
        state.update(kwargs)
        self.save(state)

    # ── Reset ───────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Efface l'état persisté (ex: en début de journée)."""
        try:
            if self._redis:
                self._redis.delete(_REDIS_KEY)
            elif self.filepath.exists():
                self.filepath.unlink()
            logger.info("State effacé.")
        except Exception as e:
            logger.error(f"Échec clear state : {e}")


# ──────────────────────────────────────────────────────────────────────────────
#  Instance globale (importée par le bot principal)
# ──────────────────────────────────────────────────────────────────────────────
state_manager = StateManager()
