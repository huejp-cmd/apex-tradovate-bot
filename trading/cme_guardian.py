"""
CME Guardian — Règles CME + Apex Trader Funding
================================================
Gère TOUTES les contraintes temporelles et de risque Apex.

Contraintes CME critiques (violation = compte grillé instantanément) :
  - Fermeture obligatoire avant 22h45 Paris (CEST) = 16h45 EDT
  - Blackout overnight : 22h45 → 00h00 Paris
  - Blackout weekend : vendredi 22h45 → lundi 00h00 Paris
  - Jours fériés US : pas de position

Contraintes Apex $50k Evaluation :
  - Profit Target      : $3 000 (+6%)
  - Max Daily Loss     : $2 500
  - Trailing Drawdown  : $2 500 (EOD — trail le highest closing balance)
  - No consistency rule (eval only — PA account a les siennes)
  - Minimum de jours   : aucun (valider dès que target atteint)

Notes sur le Trailing Drawdown Apex :
  - Il trail le highest END-OF-DAY balance (pas intraday)
  - Si balance EOD = $51 500 → floor = $49 000
  - Floor monte mais ne DESCEND JAMAIS
  - Sur compte PA : le trailing devient STATIQUE une fois le profit target atteint
"""

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  Timezone
# ─────────────────────────────────────────────────────────────────
TZ_PARIS = timezone(timedelta(hours=2))   # CEST (été) — passer à +1 en hiver
TZ_ET    = timezone(timedelta(hours=-4))  # EDT (été) — passer à -5 en hiver

# Heure de clôture CME (Paris CEST)
CLOSE_HARD_H = 22
CLOSE_HARD_M = 45   # 22h45 = limite absolue Apex (CME ferme 16h59 ET)
CLOSE_WARN_H = 22
CLOSE_WARN_M = 30   # 22h30 = début alerte → clôture en cours

# Heure de réouverture CME (Paris CEST)
OPEN_H = 0   # 00h00 Paris = 18h00 ET (CME rouvre)
OPEN_M = 5   # 5 min de marge pour ne pas traiter à l'ouverture exacte

# Jours fériés US 2026 (pas de trading — ajouter les suivants chaque année)
US_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 5, 25),   # Memorial Day
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

# ─────────────────────────────────────────────────────────────────
#  Apex $50k Evaluation — Paramètres financiers
# ─────────────────────────────────────────────────────────────────
APEX_STARTING_BALANCE = 50_000.0
APEX_PROFIT_TARGET    = 3_000.0     # +$3 000 → $53 000
APEX_MAX_DAILY_LOSS   = 2_500.0     # Stop jour absolu
APEX_TRAILING_DD      = 2_500.0     # Trailing depuis EOD HWM
APEX_DAILY_STOP_SOFT  = 1_500.0     # Stop soft (leeway $1 000 avant limite)
APEX_DAILY_PROFIT_CAP = 1_500.0     # Arrêt volontaire si +$1 500/jour (sécurité trailing DD)

class CMEGuardian:
    """
    Gardien des règles CME + Apex — singleton par account.

    Usage :
        g = CMEGuardian(starting_balance=50_000)
        allowed, reason = g.is_trading_allowed()
        g.on_day_close(closing_balance=51_200)
        must_close, reason = g.must_close_now()
    """

    def __init__(
        self,
        starting_balance: float = APEX_STARTING_BALANCE,
        profit_target:    float = APEX_PROFIT_TARGET,
        max_daily_loss:   float = APEX_MAX_DAILY_LOSS,
        trailing_dd:      float = APEX_TRAILING_DD,
        daily_stop_soft:  float = APEX_DAILY_STOP_SOFT,
        daily_profit_cap: float = APEX_DAILY_PROFIT_CAP,
    ):
        self.starting_balance = starting_balance
        self.profit_target    = profit_target
        self.max_daily_loss   = max_daily_loss
        self.trailing_dd      = trailing_dd
        self.daily_stop_soft  = daily_stop_soft
        self.daily_profit_cap = daily_profit_cap

        # Trailing drawdown state
        self._eod_hwm     = starting_balance          # Highest EOD balance ever
        self._dd_floor    = starting_balance - trailing_dd  # Floor absolu ($47 500 au départ)
        self._target_reached = False

        # Daily P&L tracking
        self._daily_pnl       = 0.0
        self._daily_date      = None
        self._day_start_balance = starting_balance

    # ──────────────────────────────────────────────
    #  Temps Paris
    # ──────────────────────────────────────────────

    @staticmethod
    def now_paris() -> datetime:
        return datetime.now(TZ_PARIS)

    @staticmethod
    def today_paris() -> date:
        return datetime.now(TZ_PARIS).date()

    # ──────────────────────────────────────────────
    #  Blackout horaire CME
    # ──────────────────────────────────────────────

    def _is_blackout_time(self) -> Tuple[bool, str]:
        """Vérifie si on est dans un blackout CME pur (horaire)."""
        now = self.now_paris()
        h, m, wd = now.hour, now.minute, now.weekday()  # 0=lundi … 6=dimanche

        # Weekend : vendredi 22h45 → lundi 00h05
        if wd == 4 and (h > CLOSE_HARD_H or (h == CLOSE_HARD_H and m >= CLOSE_HARD_M)):
            return True, "blackout_weekend_debut_vendredi"
        if wd == 5:
            return True, "blackout_samedi"
        if wd == 6:
            return True, "blackout_dimanche"
        if wd == 0 and (h < OPEN_H or (h == OPEN_H and m < OPEN_M)):
            return True, "blackout_lundi_ouverture"

        # Overnight semaine : 22h45 → 00h05
        if h == CLOSE_HARD_H and m >= CLOSE_HARD_M:
            return True, "blackout_overnight_22h45"
        if h == 23:
            return True, "blackout_overnight_23h"
        if h == 0 and m < OPEN_M:
            return True, "blackout_overnight_00h"

        return False, ""

    def _is_us_holiday(self) -> Tuple[bool, str]:
        """Vérifie si aujourd'hui est un jour férié US."""
        today = self.today_paris()
        if today in US_HOLIDAYS_2026:
            return True, f"us_holiday_{today}"
        return False, ""

    # ──────────────────────────────────────────────
    #  Clôture obligatoire
    # ──────────────────────────────────────────────

    def must_close_now(self) -> Tuple[bool, str]:
        """
        True si une clôture doit être déclenchée IMMÉDIATEMENT.
        Vérifie à intervalles réguliers (ex: toutes les 30s dans un thread).
        """
        now = self.now_paris()
        h, m, wd = now.hour, now.minute, now.weekday()

        # Fenêtre d'avertissement 22h30 → 22h45 (lun-ven)
        if wd < 5 and h == CLOSE_WARN_H and CLOSE_WARN_M <= m < CLOSE_HARD_M:
            return True, f"close_window_22h{m:02d}_paris"

        # Hard close dans blackout
        blackout, reason = self._is_blackout_time()
        if blackout:
            return True, f"hard_close_{reason}"

        return False, ""

    # ──────────────────────────────────────────────
    #  Apex financial guards
    # ──────────────────────────────────────────────

    def _daily_reset_if_needed(self, current_balance: float):
        today = self.today_paris().isoformat()
        if self._daily_date != today:
            self._daily_pnl         = 0.0
            self._daily_date        = today
            self._day_start_balance = current_balance
            log.info(f"[CMEGuardian] Nouveau jour {today}, balance={current_balance:.2f}")

    def is_daily_loss_limit_hit(self, current_balance: float) -> Tuple[bool, str]:
        """Vérifie si le daily loss limit Apex est atteint."""
        self._daily_reset_if_needed(current_balance)
        daily_pnl = current_balance - self._day_start_balance
        if daily_pnl <= -self.max_daily_loss:
            return True, f"daily_loss_limit_{abs(daily_pnl):.0f}_of_{self.max_daily_loss:.0f}"
        if daily_pnl <= -self.daily_stop_soft:
            return True, f"daily_soft_stop_{abs(daily_pnl):.0f}"
        return False, ""

    def is_daily_profit_cap_hit(self, current_balance: float) -> Tuple[bool, str]:
        """Arrêt volontaire si gain journalier dépasse le cap (protège trailing DD)."""
        self._daily_reset_if_needed(current_balance)
        daily_pnl = current_balance - self._day_start_balance
        if daily_pnl >= self.daily_profit_cap:
            return True, f"daily_profit_cap_{daily_pnl:.0f}_of_{self.daily_profit_cap:.0f}"
        return False, ""

    def is_trailing_dd_danger(self, current_balance: float) -> Tuple[bool, str]:
        """
        Vérifie si on approche dangereusement du floor trailing drawdown.
        Retourne (danger, raison) si balance < floor + $500 (marge de sécurité).
        """
        margin = current_balance - self._dd_floor
        if margin <= 0:
            return True, f"trailing_dd_BREACH_floor={self._dd_floor:.0f}_balance={current_balance:.0f}"
        if margin <= 500:
            return True, f"trailing_dd_danger_margin={margin:.0f}"
        return False, ""

    def on_day_close(self, closing_balance: float):
        """
        À appeler à chaque fin de session (22h45 Paris).
        Met à jour le trailing drawdown EOD.
        """
        today = self.today_paris().isoformat()
        if closing_balance > self._eod_hwm:
            old_hwm       = self._eod_hwm
            self._eod_hwm = closing_balance
            self._dd_floor = closing_balance - self.trailing_dd
            log.info(
                f"[CMEGuardian] EOD {today} — Nouveau HWM: {old_hwm:.2f} → {self._eod_hwm:.2f}"
                f" | Floor trailing DD: {self._dd_floor:.2f}"
            )
        else:
            log.info(
                f"[CMEGuardian] EOD {today} — Balance {closing_balance:.2f}"
                f" < HWM {self._eod_hwm:.2f} — Floor inchangé: {self._dd_floor:.2f}"
            )

        # Vérification target
        if closing_balance >= self.starting_balance + self.profit_target:
            self._target_reached = True
            log.info(f"[CMEGuardian] 🏆 PROFIT TARGET ATTEINT : {closing_balance:.2f} "
                     f"(besoin {self.starting_balance + self.profit_target:.2f})")

    def is_target_reached(self, current_balance: float) -> bool:
        return current_balance >= self.starting_balance + self.profit_target

    # ──────────────────────────────────────────────
    #  Point d'entrée principal
    # ──────────────────────────────────────────────

    def is_trading_allowed(self, current_balance: float = None) -> Tuple[bool, str]:
        """
        Vérifie toutes les règles dans l'ordre de priorité.
        Retourne (allowed, reason).
        """
        # 1. Blackout CME (priorité absolue)
        blackout, reason = self._is_blackout_time()
        if blackout:
            return False, reason

        # 2. Jour férié US
        holiday, reason = self._is_us_holiday()
        if holiday:
            return False, reason

        if current_balance is not None:
            # 3. Target atteint → arrêt (NE PAS trader au-delà du target !)
            if self.is_target_reached(current_balance):
                return False, "profit_target_reached"

            # 4. Daily loss limit
            loss_hit, reason = self.is_daily_loss_limit_hit(current_balance)
            if loss_hit:
                return False, reason

            # 5. Daily profit cap (protection trailing DD)
            cap_hit, reason = self.is_daily_profit_cap_hit(current_balance)
            if cap_hit:
                return False, reason

            # 6. Trailing DD danger
            dd_danger, reason = self.is_trailing_dd_danger(current_balance)
            if dd_danger:
                return False, reason

        return True, "ok"

    def get_status(self, current_balance: float = None) -> dict:
        """Snapshot complet de l'état guardian."""
        cb = current_balance or self._eod_hwm
        daily_pnl = cb - self._day_start_balance if self._day_start_balance else 0
        return {
            "eod_hwm":            round(self._eod_hwm, 2),
            "dd_floor":           round(self._dd_floor, 2),
            "floor_margin":       round(cb - self._dd_floor, 2),
            "target_balance":     self.starting_balance + self.profit_target,
            "target_reached":     self.is_target_reached(cb),
            "daily_pnl":          round(daily_pnl, 2),
            "daily_pnl_pct":      round(daily_pnl / self.starting_balance * 100, 2),
            "daily_date":         self._daily_date,
            "time_paris":         self.now_paris().strftime("%Y-%m-%d %H:%M:%S CEST"),
            "blackout":           self._is_blackout_time()[0],
            "us_holiday":         self._is_us_holiday()[0],
            "trading_allowed":    self.is_trading_allowed(current_balance)[0],
        }


# ─────────────────────────────────────────────────────────────────
#  Singleton global (importé partout dans le bot)
# ─────────────────────────────────────────────────────────────────
guardian = CMEGuardian()
