"""
Apex Labouchere V8 HL — Position Sizing pour Apex Trader Funding
================================================================
Adapté de la stratégie V8 HL (backtest ETH 1H : WR=74.13%, PF=4.195)

DIFFÉRENCES CLÉS vs HL Labouchere :
  - 1 unité = $50 FIXE (pas % du capital — Apex est taille fixe)
  - Contrats = entiers uniquement (pas de fraction sur CME)
  - Sizing en contrats = f(bet_units, ATR_sl, contract_value)
  - Daily loss guard intégré (Apex daily loss limit $2 500)
  - Trailing DD guard (protège le floor Apex)
  - Consistency Rule intégrée pour le PA account (30% max/jour)

LOGIQUE LABOUCHERE V8 HL :
  Sequence initiale : [2, 2, 2, 2]  (ou [1,1,1,1] mode conservateur)
  Mise courante = seq[0] + seq[-1]  (ou seq[0]*2 si singleton)
  1 unité = UNIT_VALUE = $50

  LOSS → retire seq[0] et seq[-1] | si vide → reset [seqInit x4]
  WIN (mise < SPLIT_THRESHOLD) → ajoute [mise] à la fin
  WIN (mise >= SPLIT_THRESHOLD, < MAX_BET_UNITS) → divise par 3 → ajoute [a,b,c]
  WIN (mise >= MAX_BET_UNITS) → diviseur croissant (/4, /5, /6...)

  Plafond absolu : MAX_BET_UNITS = 12 (= $600 risque max par trade)
  Seuil split     : SPLIT_THRESHOLD = 6 unités

SIZING CONTRATS — METH (Micro ETH CME) :
  1 METH = 0.1 ETH
  Si ETH prix = $1 800 → 1 METH notional = $180
  Tick size = $0.05/ETH → tick value = $0.005/METH (très granulaire ✓)
  
  Formule contrats = max(1, round(bet_usd / (atr_sl_eth * 0.1 * price)))
  où atr_sl_eth = ATR_usd / current_price (mouvement de prix en ETH)

  Exemple : bet=4u=$200, ATR=$80, ETH=$1800
    → atr_sl_eth = 80 (en $, pas en ETH — c'est le $ move de l'actif)
    → contrats = round(200 / (80 * 0.1)) = round(200/8) = 25 METH

VALIDATION $50k EN 3 JOURS :
  Avec WR=74.13%, PF=4.195, ~5 trades/jour :
  - Séquence [2,2,2,2] × $75/unité : EV/trade ≈ $248, total 15 trades ≈ $3 720
  - Séquence [2,2,2,2] × $50/unité : EV/trade ≈ $165, total 15 trades ≈ $2 475 (limite basse)
  → Recommandé : $75/unité pour validation sure en 3 jours
  → Mode conservateur post-validation : $50/unité
"""

import json
import logging
import math
import os
import threading
from datetime import datetime, timezone, timedelta, date
from typing import List, Tuple, Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  Persistance
# ─────────────────────────────────────────────────────────────────
_HERE        = os.path.dirname(os.path.abspath(__file__))
_PERSIST_DIR = os.environ.get("PERSIST_DIR", "/data")

def _resolve_path(filename: str) -> str:
    for d in [_PERSIST_DIR, "/app", "/tmp", _HERE]:
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".probe")
            open(t, "w").write("x"); os.remove(t)
            return os.path.join(d, filename)
        except Exception:
            continue
    return os.path.join(_HERE, filename)

STATE_FILE   = _resolve_path("apex_v8hl_state.json")
HISTORY_FILE = _resolve_path("apex_v8hl_history.json")

# ─────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────

# ── Modes de validation ──
MODE_VALIDATION  = "validation"   # Eval 3 jours — unit=$75, seq=[2,2,2,2]
MODE_PA_NORMAL   = "pa_normal"    # PA standard — unit=$50, seq=[2,2,2,2]
MODE_PA_CONSERV  = "pa_conserv"  # PA prudent  — unit=$50, seq=[1,1,1,1]

UNIT_VALUE = {
    MODE_VALIDATION : 75.0,   # $75/unité → EV/trade ≈ $248, hits $3k en ~3j
    MODE_PA_NORMAL  : 50.0,   # $50/unité standard
    MODE_PA_CONSERV : 50.0,
}

INIT_SEQ = {
    MODE_VALIDATION : [2, 2, 2, 2],
    MODE_PA_NORMAL  : [2, 2, 2, 2],
    MODE_PA_CONSERV : [1, 1, 1, 1],
}

SPLIT_THRESHOLD = 6    # Seuil split WIN : mise ≥ 6 unités → diviser par 3
MAX_BET_UNITS   = 12   # Plafond absolu = $600 risque max (sécurité Apex trailing DD)
MIN_BET_UNITS   = 2    # Mise minimum

# Apex $50k constraints
APEX_MAX_DAILY_LOSS  = 2_500.0   # Daily loss limit absolu
APEX_DAILY_SOFT_STOP = 1_500.0   # Soft stop (laisse $1k de buffer)
APEX_MAX_DAY_PROFIT  = 1_500.0   # Cap journalier volontaire (protège trailing DD)

# METH (Micro ETH CME) — contrat de référence
METH_CONTRACT_SIZE   = 0.1       # 0.1 ETH par contrat
METH_TICK_SIZE_USD   = 0.05      # 0.05 $ par ETH (tick)
METH_TICK_VALUE      = METH_CONTRACT_SIZE * METH_TICK_SIZE_USD  # = $0.005/tick/contrat
METH_MAX_CONTRACTS   = 200       # Limite de sécurité (ne pas dépasser)

# Consistency Rule (PA account uniquement)
PA_CONSISTENCY_MAX_PCT = 0.30    # Max 30% des profits totaux en 1 jour

# ─────────────────────────────────────────────────────────────────
#  Timezone Paris
# ─────────────────────────────────────────────────────────────────
TZ_PARIS = timezone(timedelta(hours=2))  # CEST

def _now_paris() -> str:
    return datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M:%S")

def _today_paris() -> str:
    return datetime.now(TZ_PARIS).strftime("%Y-%m-%d")

# ─────────────────────────────────────────────────────────────────
#  État par défaut
# ─────────────────────────────────────────────────────────────────
def _default_state(mode: str = MODE_VALIDATION) -> dict:
    return {
        "mode":           mode,
        "unit_value":     UNIT_VALUE[mode],
        "sequence":       list(INIT_SEQ[mode]),
        "split_counter":  0,   # Compte les splits (diviseur croissant)

        # Stats globales
        "wins":           0,
        "losses":         0,
        "cycles":         0,    # Nb de resets séquence
        "cum_pnl":        0.0,

        # Daily tracking
        "daily_pnl":      0.0,
        "daily_date":     None,
        "daily_wins":     0,
        "daily_losses":   0,

        # PA consistency rule
        "pa_total_profit": 0.0,  # Profits totaux depuis début PA (pour rule 30%)
        "pa_enabled":      False,
    }

# ─────────────────────────────────────────────────────────────────
#  I/O
# ─────────────────────────────────────────────────────────────────
_lock = threading.Lock()

def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return _default_state()

def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _load_history() -> List[dict]:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _save_history(h: List[dict]):
    with open(HISTORY_FILE, "w") as f:
        json.dump(h, f, indent=2)

# ─────────────────────────────────────────────────────────────────
#  Logique Labouchere V8 HL
# ─────────────────────────────────────────────────────────────────

def _get_bet_units(seq: List[int]) -> int:
    """Calcule la mise courante depuis la séquence."""
    if not seq:
        return MIN_BET_UNITS
    if len(seq) == 1:
        return min(seq[0] * 2, MAX_BET_UNITS)
    return min(seq[0] + seq[-1], MAX_BET_UNITS)

def _apply_win(state: dict, bet_units: int) -> dict:
    """
    WIN : met à jour la séquence selon la logique V8 HL.
    Retourne un dict décrivant ce qui a été ajouté.
    """
    seq = state["sequence"]

    if bet_units < SPLIT_THRESHOLD:
        # WIN normal : ajoute [mise] à la fin
        seq.append(bet_units)
        added = [bet_units]
        split_used = False
    elif bet_units < MAX_BET_UNITS:
        # WIN gros : divise par 3 + diviseur croissant si répété
        divisor = 3 + state.get("split_counter", 0)
        parts_base = max(1, bet_units // divisor)
        remainder  = bet_units % divisor
        parts = [parts_base] * (divisor - 1) + [parts_base + remainder]
        parts = [min(p, MAX_BET_UNITS) for p in parts]
        seq.extend(parts)
        added = parts
        state["split_counter"] = state.get("split_counter", 0) + 1
        split_used = True
        log.info(f"[Lab V8] WIN big (bet={bet_units}≥{SPLIT_THRESHOLD}) "
                 f"→ split ÷{divisor} → {parts}")
    else:
        # WIN au plafond : split maximal (diviseur = 4+ croissant)
        divisor = 4 + state.get("split_counter", 0)
        part = max(2, bet_units // divisor)
        parts = [part, part, part]
        seq.extend(parts)
        added = parts
        state["split_counter"] = state.get("split_counter", 0) + 1
        split_used = True
        log.info(f"[Lab V8] WIN plafond (bet={bet_units}≥{MAX_BET_UNITS}) "
                 f"→ split ÷{divisor} → {parts}")

    state["sequence"] = seq
    return {"added": added, "split": split_used}

def _apply_loss(state: dict) -> dict:
    """
    LOSS : retire premier + dernier. Reset si séquence vide.
    """
    seq = state["sequence"]
    seq_before = list(seq)

    if len(seq) >= 2:
        state["sequence"] = seq[1:-1]
    elif len(seq) == 1:
        state["sequence"] = []

    reset = False
    if not state["sequence"]:
        mode = state.get("mode", MODE_VALIDATION)
        state["sequence"] = list(INIT_SEQ.get(mode, [2, 2, 2, 2]))
        state["cycles"]  += 1
        state["split_counter"] = 0  # Reset le compteur de splits
        reset = True
        log.info(f"[Lab V8] LOSS → séquence épuisée → RESET [{state['sequence']}]")

    return {"reset": reset, "seq_before": seq_before}

# ─────────────────────────────────────────────────────────────────
#  Sizing Contrats METH
# ─────────────────────────────────────────────────────────────────

def calc_meth_contracts(
    bet_units:     int,
    unit_value:    float,
    atr_sl_usd:    float,
    eth_price:     float,
    min_contracts: int = 1,
) -> int:
    """
    Calcule le nombre de contrats METH à trader.

    Formule :
      risk_usd = bet_units * unit_value
      risk_per_contract = atr_sl_usd * METH_CONTRACT_SIZE
        (car 1 METH = 0.1 ETH, et le SL est en $ de mouvement ETH)
      contracts = risk_usd / risk_per_contract

    Args:
      bet_units    : nombre d'unités (ex: 4)
      unit_value   : valeur d'une unité en $ (ex: $75)
      atr_sl_usd   : distance du stop-loss en $ ETH price move (ex: $80)
      eth_price    : prix ETH courant (pour logging uniquement)
      min_contracts: minimum forcé (défaut 1)

    Exemple :
      bet=4u, unit=$75 → risk=$300
      atr_sl=$80 → risk/contrat = $80 * 0.1 = $8
      contracts = 300 / 8 = 37.5 → 38 METH

    Pourquoi METH est idéal :
      - risk/contrat très petit ($3-15 selon ATR) → précision fine
      - Pas d'arrondi grossier comme MNQ ($300/contrat)
    """
    if atr_sl_usd <= 0 or eth_price <= 0:
        log.warning(f"[Lab V8] calc_meth_contracts: atr_sl={atr_sl_usd}, price={eth_price} invalide")
        return min_contracts

    risk_usd         = bet_units * unit_value
    risk_per_contract = atr_sl_usd * METH_CONTRACT_SIZE  # $move * 0.1 ETH

    if risk_per_contract <= 0:
        return min_contracts

    contracts = max(min_contracts, round(risk_usd / risk_per_contract))
    contracts = min(contracts, METH_MAX_CONTRACTS)  # Plafond de sécurité

    log.info(
        f"[Lab V8] Sizing METH: bet={bet_units}u×${unit_value}=${risk_usd:.0f} "
        f"| ATR_SL=${atr_sl_usd:.1f} | risk/ctr=${risk_per_contract:.2f} "
        f"| ETH=${eth_price:.0f} → {contracts} METH"
    )
    return contracts

# ─────────────────────────────────────────────────────────────────
#  Guards journaliers Apex
# ─────────────────────────────────────────────────────────────────

def _daily_reset_if_needed(state: dict):
    today = _today_paris()
    if state.get("daily_date") != today:
        state["daily_pnl"]    = 0.0
        state["daily_date"]   = today
        state["daily_wins"]   = 0
        state["daily_losses"] = 0
        log.info(f"[Lab V8] Nouveau jour {today}")

def check_can_trade(state: dict, current_balance: float) -> Tuple[bool, str]:
    """
    Vérifie les guards financiers Apex.
    Ne vérifie PAS les horaires CME (géré par CMEGuardian).
    """
    _daily_reset_if_needed(state)
    daily_pnl = state.get("daily_pnl", 0.0)

    # 1. Daily loss hard stop
    if daily_pnl <= -APEX_MAX_DAILY_LOSS:
        return False, f"apex_daily_loss_limit: {daily_pnl:.0f}$"

    # 2. Daily soft stop (buffer $1k avant limite)
    if daily_pnl <= -APEX_DAILY_SOFT_STOP:
        return False, f"apex_daily_soft_stop: {daily_pnl:.0f}$"

    # 3. Daily profit cap (protège trailing DD)
    if daily_pnl >= APEX_MAX_DAY_PROFIT:
        return False, f"apex_daily_profit_cap: {daily_pnl:.0f}$"

    # 4. PA Consistency Rule (30% max des profits totaux par jour)
    if state.get("pa_enabled") and state.get("pa_total_profit", 0) > 0:
        max_day = state["pa_total_profit"] * PA_CONSISTENCY_MAX_PCT
        if daily_pnl >= max_day:
            return False, f"pa_consistency_rule: {daily_pnl:.0f}$ > 30% of {state['pa_total_profit']:.0f}$"

    return True, "ok"

# ─────────────────────────────────────────────────────────────────
#  API publique
# ─────────────────────────────────────────────────────────────────

def record_result(
    side:        str,
    result:      str,      # "WIN" ou "LOSS"
    pnl:         float,    # P&L en $
    contracts:   int,      # Nb contrats tradés
    entry_price: float,
    exit_price:  float,
    atr_sl:      float,    # ATR SL utilisé ($)
    signal_info: dict = None,
) -> dict:
    """
    Enregistre un trade et met à jour la séquence Labouchere.
    Retourne l'entrée de journal complète.
    """
    with _lock:
        state   = _load_state()
        history = _load_history()

        _daily_reset_if_needed(state)

        seq_before = list(state["sequence"])
        bet_before = _get_bet_units(seq_before)
        unit_value = state.get("unit_value", UNIT_VALUE[MODE_VALIDATION])
        risk_usd   = bet_before * unit_value

        if result == "WIN":
            state["wins"]         += 1
            state["daily_wins"]   += 1
            win_info = _apply_win(state, bet_before)
            reset    = False
        else:
            state["losses"]       += 1
            state["daily_losses"] += 1
            loss_info = _apply_loss(state)
            win_info  = {}
            reset     = loss_info.get("reset", False)

        seq_after  = list(state["sequence"])
        bet_after  = _get_bet_units(seq_after)

        state["cum_pnl"]   += pnl
        state["daily_pnl"] += pnl

        if state.get("pa_enabled"):
            if pnl > 0:
                state["pa_total_profit"] = state.get("pa_total_profit", 0.0) + pnl

        entry_log = {
            "ts":          _now_paris(),
            "date":        _today_paris(),
            "side":        side,
            "result":      result,
            "contracts":   contracts,
            "entry":       entry_price,
            "exit":        exit_price,
            "atr_sl":      atr_sl,
            "pnl":         round(pnl, 2),
            "seq_before":  seq_before,
            "bet_before":  bet_before,
            "risk_usd":    risk_usd,
            "seq_after":   seq_after,
            "bet_after":   bet_after,
            "bet_usd_after": bet_after * unit_value,
            "added":       win_info.get("added", []),
            "split_used":  win_info.get("split", False),
            "reset":       reset,
            "daily_pnl":  round(state["daily_pnl"], 2),
            "cum_pnl":    round(state["cum_pnl"], 2),
            "cum_wins":   state["wins"],
            "cum_losses": state["losses"],
            "cycles":     state["cycles"],
            "signal":     signal_info or {},
        }
        history.append(entry_log)
        _save_state(state)
        _save_history(history)

        log.info(
            f"[Lab V8] {'✅' if result=='WIN' else '❌'} {result} "
            f"P&L={pnl:+.0f}$ | seq {seq_before}→{seq_after} "
            f"| next_bet={bet_after}u=${bet_after*unit_value:.0f} "
            f"| daily={state['daily_pnl']:+.0f}$ cum={state['cum_pnl']:+.0f}$"
        )
        return entry_log


def get_current_bet(atr_sl_usd: float = 60.0, eth_price: float = 1800.0) -> dict:
    """
    Retourne la mise courante avec le nombre de contrats METH calculé.
    C'est l'appel principal AVANT de placer un ordre.
    """
    with _lock:
        state      = _load_state()
        seq        = state["sequence"]
        bet_units  = _get_bet_units(seq)
        unit_value = state.get("unit_value", UNIT_VALUE[MODE_VALIDATION])
        risk_usd   = bet_units * unit_value
        contracts  = calc_meth_contracts(bet_units, unit_value, atr_sl_usd, eth_price)

        return {
            "sequence":    seq,
            "bet_units":   bet_units,
            "unit_value":  unit_value,
            "risk_usd":    risk_usd,
            "contracts":   contracts,
            "atr_sl_usd":  atr_sl_usd,
            "eth_price":   eth_price,
            "mode":        state.get("mode", MODE_VALIDATION),
        }


def get_state_summary() -> dict:
    """Snapshot complet pour le dashboard."""
    with _lock:
        state  = _load_state()
        seq    = state["sequence"]
        bet    = _get_bet_units(seq)
        unit   = state.get("unit_value", UNIT_VALUE[MODE_VALIDATION])
        total  = state["wins"] + state["losses"]
        wr     = round(state["wins"] / total * 100, 1) if total > 0 else 0.0

        # Estimation jours pour atteindre $3k (basé sur EV avec WR historique 74.13%)
        avg_win_r  = 1.464          # R-ratio moyen des wins (depuis PF=4.195, WR=74.13%)
        epp        = (0.7413 * bet * unit * avg_win_r) - (0.2587 * bet * unit)
        trades_5pd = 5              # ~5 trades/jour sur ETH 1H
        days_to_3k = 3000 / max(epp * trades_5pd, 1) if epp > 0 else 999

        return {
            "mode":            state.get("mode"),
            "sequence":        seq,
            "seq_length":      len(seq),
            "bet_units":       bet,
            "unit_value":      unit,
            "risk_per_trade":  bet * unit,
            "wins":            state["wins"],
            "losses":          state["losses"],
            "win_rate":        wr,
            "cycles":          state["cycles"],
            "daily_pnl":       round(state.get("daily_pnl", 0.0), 2),
            "cum_pnl":         round(state.get("cum_pnl", 0.0), 2),
            "daily_date":      state.get("daily_date"),
            "pa_enabled":      state.get("pa_enabled", False),
            "pa_total_profit": round(state.get("pa_total_profit", 0.0), 2),
            "epp_estimate":    round(epp, 2),  # Expected P&L par trade
            "days_to_3k_est":  round(days_to_3k, 1),
        }


def set_mode(mode: str):
    """Change le mode (validation → pa_normal, etc.)"""
    if mode not in (MODE_VALIDATION, MODE_PA_NORMAL, MODE_PA_CONSERV):
        raise ValueError(f"Mode inconnu: {mode}")
    with _lock:
        state = _load_state()
        state["mode"]       = mode
        state["unit_value"] = UNIT_VALUE[mode]
        # Ne reset pas la séquence — continue en cours
        _save_state(state)
    log.info(f"[Lab V8] Mode changé → {mode} (unit=${UNIT_VALUE[mode]})")


def reset_sequence(mode: str = None):
    """Reset complet de la séquence (garder l'historique P&L)."""
    with _lock:
        state = _load_state()
        m = mode or state.get("mode", MODE_VALIDATION)
        state["sequence"]      = list(INIT_SEQ[m])
        state["split_counter"] = 0
        _save_state(state)
    log.info(f"[Lab V8] Séquence RESET → {INIT_SEQ[m]}")


def enable_pa_mode(current_profit: float = 0.0):
    """Active le PA mode avec la Consistency Rule."""
    with _lock:
        state = _load_state()
        state["pa_enabled"]      = True
        state["pa_total_profit"] = current_profit
        state["mode"]            = MODE_PA_NORMAL
        state["unit_value"]      = UNIT_VALUE[MODE_PA_NORMAL]
        _save_state(state)
    log.info(f"[Lab V8] PA mode activé | total_profit={current_profit:.2f}$")


def get_history(date_filter: str = None, limit: int = 50) -> List[dict]:
    with _lock:
        h = _load_history()
        if date_filter:
            h = [e for e in h if e.get("date") == date_filter]
        return h[-limit:]


# ─────────────────────────────────────────────────────────────────
#  Calcul Validation 3 jours — Helper analytique
# ─────────────────────────────────────────────────────────────────

def estimate_validation_days(
    unit_value:    float = 75.0,
    init_seq:      list  = None,
    wr:            float = 0.7413,
    pf:            float = 4.195,
    trades_per_day: int  = 5,
    target_pnl:    float = 3_000.0,
) -> dict:
    """
    Estimation analytique du temps de validation.
    Utilise la formule EV avec Labouchere linéarisé (approximation).
    """
    if init_seq is None:
        init_seq = [2, 2, 2, 2]

    bet_units  = init_seq[0] + init_seq[-1]  # Mise initiale
    avg_win_r  = pf * (1 - wr) / wr          # R-ratio moyen des winners
    risk_usd   = bet_units * unit_value

    # EV par trade (approximation sans compounding)
    avg_win_usd  = risk_usd * avg_win_r
    epp_base     = wr * avg_win_usd - (1 - wr) * risk_usd

    # Facteur compounding Labouchere (~+30% avec séquence croissante sur winning runs)
    lab_boost    = 1.30
    epp_lab      = epp_base * lab_boost

    # Nombre de trades et jours
    trades_needed = target_pnl / max(epp_lab, 0.01)
    days_needed   = trades_needed / trades_per_day

    # Worst case (P5 : perte journalière max avec 6 pertes consécutives)
    max_consec_losses = 6
    max_loss_per_day  = (max_consec_losses // 2) * 2 * bet_units * unit_value  # 3 resets

    return {
        "unit_value":        unit_value,
        "init_seq":          init_seq,
        "bet_units_start":   bet_units,
        "risk_per_trade":    f"${risk_usd:.0f}",
        "wr":                f"{wr*100:.1f}%",
        "avg_win_r":         f"{avg_win_r:.3f}R",
        "avg_win_usd":       f"${avg_win_usd:.0f}",
        "epp_base":          f"${epp_base:.0f}/trade",
        "epp_with_lab":      f"${epp_lab:.0f}/trade",
        "trades_per_day":    trades_per_day,
        "epp_per_day":       f"${epp_lab * trades_per_day:.0f}/jour",
        "trades_to_target":  f"{trades_needed:.0f} trades",
        "days_to_target":    f"{days_needed:.1f} jours",
        "max_loss_6_streak": f"${max_loss_per_day:.0f}",
        "daily_loss_safe":   max_loss_per_day <= 2_500,
        "target_pnl":        f"${target_pnl:.0f}",
    }


if __name__ == "__main__":
    # ── Simulations de validation ──
    print("\n=== SIMULATION VALIDATION APEX $50k ===\n")
    for mode_name, uv, seq in [
        ("Conservateur [1,1,1,1] ×$50", 50, [1,1,1,1]),
        ("Standard    [2,2,2,2] ×$50", 50, [2,2,2,2]),
        ("Validation  [2,2,2,2] ×$75", 75, [2,2,2,2]),
        ("Agressif    [2,2,2,2] ×$100",100, [2,2,2,2]),
    ]:
        est = estimate_validation_days(unit_value=uv, init_seq=seq)
        print(f"{'─'*55}")
        print(f"Mode : {mode_name}")
        print(f"  Mise initiale    : {est['risk_per_trade']}")
        print(f"  EV/trade (lab)   : {est['epp_with_lab']}")
        print(f"  EV/jour (5T)     : {est['epp_per_day']}")
        print(f"  Temps estimé     : {est['days_to_target']}")
        print(f"  Worst loss 6str  : {est['max_loss_6_streak']} {'✅' if est['daily_loss_safe'] else '❌ DEPASSE $2500'}")
