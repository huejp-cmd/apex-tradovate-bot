"""
atr_range_builder.py — Pilier 2 (partiel) + Logique ATR/Range Bar
==================================================================
Calcule la taille des Range Bars via ATR sur 3 timeframes (5m, 10m, 15m).
Si aucun timeframe ne donne une taille ≥ 10 pts → pause trading.

Inclut aussi le RangeBarBuilder : construit des bougies Range Bar
à partir du flux de ticks (WebSocket Tradovate).

Usage :
    from atr_range_builder import ATRRangeSelector, RangeBarBuilder

    selector = ATRRangeSelector(tradovate_client)
    range_size = await selector.select()          # None = pause

    builder = RangeBarBuilder(range_size=12.0)
    bar = builder.on_tick(price=21340.0)          # retourne la bougie fermée ou None
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import httpx

logger = logging.getLogger("atr_range")

# ──────────────────────────────────────────────────────────────────────────────
#  Structures de données
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RangeBar:
    """Bougie Range Bar fermée."""
    open:    float
    high:    float
    low:     float
    close:   float
    ticks:   int                    # Nombre de ticks dans cette bougie
    opened_at:  datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at:  Optional[datetime] = None
    direction:  str = ""            # "bull" ou "bear"

    @property
    def is_bull(self) -> bool:
        return self.close >= self.open

    @property
    def is_bear(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)


# ──────────────────────────────────────────────────────────────────────────────
#  RangeBarBuilder — construit les bougies depuis les ticks
# ──────────────────────────────────────────────────────────────────────────────

class RangeBarBuilder:
    """
    Construit des bougies Range Bar à partir d'un flux de prix.
    Chaque bougie se ferme quand le prix a bougé de `range_size` points.

    NQ : 1 point = 4 ticks (tick size = 0.25)
    Une Range Bar de 9R = 9 × range_size pts
    """

    def __init__(self, range_size: float):
        """
        range_size : taille de chaque Range Bar en points NQ
                     (calculée par ATRRangeSelector)
        """
        self.range_size = range_size
        self._reset()
        logger.info(f"RangeBarBuilder initialisé — range_size={range_size:.2f} pts")

    def _reset(self):
        self._bar_open:  Optional[float] = None
        self._bar_high:  float = float("-inf")
        self._bar_low:   float = float("inf")
        self._tick_count = 0
        self._bar_open_time = datetime.now(timezone.utc)

    def update_range(self, new_range_size: float) -> None:
        """Met à jour la taille de Range Bar (appelé si ATR change)."""
        if new_range_size != self.range_size:
            logger.info(f"Range Bar mise à jour : {self.range_size:.2f} → {new_range_size:.2f} pts")
            self.range_size = new_range_size

    def on_tick(self, price: float) -> Optional[RangeBar]:
        """
        Traite un nouveau tick.
        Retourne une RangeBar fermée si la taille est atteinte, sinon None.
        """
        self._tick_count += 1

        # Première bougie : initialisation avec le premier prix
        if self._bar_open is None:
            self._bar_open = price
            self._bar_high = price
            self._bar_low  = price
            return None

        # Mise à jour High/Low
        self._bar_high = max(self._bar_high, price)
        self._bar_low  = min(self._bar_low,  price)

        # Vérifier si la bougie est fermée (range atteint)
        bar_range = self._bar_high - self._bar_low
        if bar_range >= self.range_size:
            closed_bar = RangeBar(
                open       = self._bar_open,
                high       = self._bar_high,
                low        = self._bar_low,
                close      = price,
                ticks      = self._tick_count,
                opened_at  = self._bar_open_time,
                closed_at  = datetime.now(timezone.utc),
                direction  = "bull" if price >= self._bar_open else "bear",
            )
            logger.debug(
                f"Range Bar fermée — O:{closed_bar.open:.2f} H:{closed_bar.high:.2f} "
                f"L:{closed_bar.low:.2f} C:{closed_bar.close:.2f} "
                f"({closed_bar.direction}) {self._tick_count} ticks"
            )
            self._reset()
            # Initialiser la nouvelle bougie avec le prix courant
            self._bar_open = price
            self._bar_high = price
            self._bar_low  = price
            return closed_bar

        return None


# ──────────────────────────────────────────────────────────────────────────────
#  ATRRangeSelector — calcule la taille via ATR multi-timeframe
# ──────────────────────────────────────────────────────────────────────────────

class ATRRangeSelector:
    """
    Sélectionne la taille des Range Bars via ATR(40) sur 5M, 10M, 15M.
    Si ATR × 0.5 ≥ 10 pts sur au moins un timeframe → trading autorisé.
    Sinon → marché pas assez volatil → pause.

    Version JP (Gemini) : adaptée pour l'API Tradovate REST.
    """

    TIMEFRAMES_AUTORISES = [5, 10, 15]   # minutes
    ATR_PERIOD           = 40
    RANGE_PLANCHER       = 10.0          # pts minimum
    ATR_RATIO            = 0.5           # range_size = ATR × 0.5

    def __init__(self, http_client=None):
        """
        http_client : instance httpx.AsyncClient avec headers d'auth
                      (ou None pour récupérer les données autrement)
        """
        self.http_client = http_client
        self.range_size:   Optional[float] = None
        self.market_active: bool = False
        self.validated_tf:  Optional[int]  = None

    async def select(self, symbol: str = "NQ") -> Optional[float]:
        """
        Parcourt les timeframes et sélectionne la première taille ≥ plancher.
        Retourne range_size si marché actif, None si volatilité insuffisante.
        """
        self.market_active = False
        self.range_size    = None
        self.validated_tf  = None

        for tf in self.TIMEFRAMES_AUTORISES:
            atr = await self._get_atr(symbol=symbol, timeframe_minutes=tf)
            if atr is None:
                logger.warning(f"ATR {tf}m indisponible — essai suivant.")
                continue

            calcul = atr * self.ATR_RATIO
            logger.info(f"ATR({self.ATR_PERIOD}) sur {tf}M = {atr:.2f} → calcul = {calcul:.2f} pts")

            if calcul >= self.RANGE_PLANCHER:
                self.range_size    = calcul
                self.market_active = True
                self.validated_tf  = tf
                logger.info(f"✅ TF validé : {tf}M | Range Bar = {calcul:.2f} pts")
                return calcul

        # Aucun TF ne valide le plancher
        logger.warning(
            f"VOLATILITÉ INSUFFISANTE (max {self.TIMEFRAMES_AUTORISES[-1]}M atteint). "
            f"Attente du prochain cycle ATR."
        )
        return None

    async def _get_atr(self, symbol: str, timeframe_minutes: int) -> Optional[float]:
        """
        Récupère l'ATR depuis Tradovate (endpoint /md/getChart).
        Calcule ATR(40) manuellement si l'API ne le fournit pas directement.

        Tradovate utilise des unités de temps différentes :
            1 = 1 minute, 5 = 5 minutes, etc.
        """
        if self.http_client is None:
            # Fallback : valeur par défaut raisonnable (ATR NQ ≈ 20-30 pts sur 10M)
            fallback_atr = {5: 15.0, 10: 22.0, 15: 28.0}.get(timeframe_minutes, 20.0)
            logger.debug(f"HTTP client absent — ATR {timeframe_minutes}M fallback = {fallback_atr}")
            return fallback_atr

        try:
            # Récupérer les dernières bougies OHLC
            payload = {
                "symbol":         symbol,
                "chartDescription": {
                    "underlyingType": "MinuteBar",
                    "elementSize":     timeframe_minutes,
                    "elementSizeUnit": "UnderlyingUnits",
                    "withHistogram":   False,
                },
                "timeRange": {
                    "asFarAsTimestamp": None,
                    "closestTimestamp": None,
                    "closestTickId":    None,
                    "asMuchAsElements": self.ATR_PERIOD + 5,  # quelques barres de marge
                },
            }
            resp = await self.http_client.post("/md/getChart", json=payload)
            if resp.status_code != 200:
                logger.warning(f"getChart {timeframe_minutes}M → HTTP {resp.status_code}")
                return None

            bars = resp.json().get("bars", [])
            if len(bars) < self.ATR_PERIOD:
                logger.warning(f"Pas assez de barres {timeframe_minutes}M ({len(bars)} < {self.ATR_PERIOD})")
                return None

            # Calcul ATR(N) de Wilder sur les N dernières barres
            return self._calc_atr(bars[-self.ATR_PERIOD:])

        except Exception as e:
            logger.error(f"Erreur _get_atr({timeframe_minutes}M) : {e}")
            return None

    @staticmethod
    def _calc_atr(bars: list) -> float:
        """
        Calcul de l'ATR de Wilder.
        bars : liste de dicts avec clés 'high', 'low', 'close'
        """
        if not bars:
            return 0.0

        trs = []
        for i, bar in enumerate(bars):
            h = float(bar.get("high",  bar.get("h", 0)))
            l = float(bar.get("low",   bar.get("l", 0)))
            c = float(bar.get("close", bar.get("c", 0)))
            prev_c = float(bars[i-1].get("close", bars[i-1].get("c", c))) if i > 0 else c

            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)

        # Moyenne simple (première valeur ATR)
        atr = sum(trs) / len(trs)
        return atr
