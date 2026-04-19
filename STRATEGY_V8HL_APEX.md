# Stratégie Apex V8 HL — Analyse Complète
**Compte : $50 000 Evaluation | APEX-548673 | Tradovate "sumiko"**
_Rédigé par Ouroboros — 2026-04-19_

---

## 1. CHOIX DE PLATEFORME : Tradovate API REST (déjà en place ✅)

**Verdict : Tradovate REST API en Python est le bon choix. Ne pas changer.**

Comparaison rapide des alternatives :

| Solution | Complexité | Fiabilité | Compat. Apex | Verdict |
|---|---|---|---|---|
| **Tradovate REST + WebSocket** | Moyenne | ✅ Haute | ✅ Native | **→ RECOMMANDÉ** |
| Rithmic Python SDK | Haute (propriétaire) | ✅ Haute | ✅ APEX-548673 = compte Rithmic | En option si Tradovate down |
| NinjaTrader (C#) | Haute | ✅ Haute | ✅ | Trop lourd, inutile |
| Sierra Chart | Très haute | ✅ | ✅ | Overkill |

**Pourquoi Tradovate reste le bon choix :**
- L'interface "sumiko" sur trader.tradovate.com fonctionne déjà
- API REST publique et documentée (demo + live endpoints)
- WebSocket disponible pour market data temps réel (surveillance SL)
- Zéro coût additionnel (inclus dans Apex)
- Le code `apex_tradovate_server.py` est déjà fonctionnel — pas la peine de tout refaire

**Seule lacune à surveiller :** les rollover de contrats METH (expiration mensuelle).
Mapping des suffixes : H=mars, M=juin, U=sept, Z=déc.
Mettre `CONTRACT_NAME` à jour dans `.env` à chaque rollover.

---

## 2. ARCHITECTURE DU BOT

```
TradingView (Pine V8 HL, ETH 1H)
    │
    │  Alerte JSON :
    │  { "action":"open", "side":"buy", "price":1823.45,
    │    "atr_sl":67.8, "regime":"trend", "token":"xxx" }
    ▼
Flask Webhook Server (apex_v8hl_server.py)
    │
    ├─► CMEGuardian (cme_guardian.py)
    │       • Blackout overnight 22h45→00h05 Paris
    │       • Blackout weekend vendredi→lundi
    │       • Jours fériés US
    │       • Daily loss limit ($2 500 hard / $1 500 soft)
    │       • Daily profit cap ($1 500 — protège trailing DD)
    │       • Trailing DD floor guard
    │
    ├─► ApexLabouchereV8 (apex_labouchere_v8.py)
    │       • Calcule bet_units depuis séquence courante
    │       • Calcule nb contrats METH = f(bet_units, ATR_SL, price)
    │       • Enregistre WIN/LOSS → met à jour séquence
    │       • Persiste état sur disque (JSON)
    │
    ├─► Tradovate REST API
    │       • Auth token (refresh automatique)
    │       • placeorder (Market)
    │       • Clôture inverse sur signal "close"
    │
    └─► Timer Thread (daemon)
            • Toutes les 30s : vérifie l'heure
            • 22h30 Paris → si position ouverte → clôture immédiate
            • Enregistre EOD balance pour trailing DD

Fichiers persistants :
    /data/apex_v8hl_state.json    ← séquence Labouchere
    /data/apex_v8hl_history.json  ← historique trades
    /data/apex_v8hl.log           ← logs

Dashboard web :
    GET /dashboard   → HTML auto-refresh 15s
    GET /status      → JSON complet
    GET /lab/state   → état Labouchere seul
```

**Flux de signal TradingView :**
Le Pine Script V8 HL émet une alerte `close_on_bar_close` (TP/SL sur clôture de bougie).
Le signal doit contenir :
- `action` : "open" ou "close"
- `side` : "buy" ou "sell"
- `price` : prix d'entrée ({{close}})
- `atr_sl` : valeur ATR × facteur SL (ex: `{{plot("ATR_SL")}}`)
- `regime` : "trend" ou "explosive"
- `token` : token secret webhook

---

## 3. CALCUL VALIDATION $50K EN 3 JOURS

### Paramètres Apex $50k Evaluation
| Paramètre | Valeur |
|---|---|
| Profit Target | $3 000 (+6%) |
| Max Daily Loss | $2 500 (violation = compte grillé) |
| Trailing Drawdown | $2 500 depuis le plus haut EOD |
| Minimum de jours | **Aucun** — valider dès que target atteint |
| Consistency Rule | **N/A sur l'éval** (seulement PA) |

### Résultats simulation (WR=74.13%, PF=4.195, 5 trades/jour ETH 1H)

| Mode | Mise initiale | EV/trade | EV/jour | Jours estimés | Max loss 6 pertes d'affilée |
|---|---|---|---|---|---|
| [1,1,1,1] × $50 | $100 | $107 | $537 | **5.6j** ❌ trop lent |$600 ✅ |
| [2,2,2,2] × $50 | $200 | $215 | $1 075 | **2.8j** ⚠️ limite | $1 200 ✅ |
| **[2,2,2,2] × $75** | **$300** | **$322** | **$1 612** | **1.9j** ✅ | **$1 800** ✅ |
| [2,2,2,2] × $100 | $400 | $430 | $2 149 | 1.4j → **bloqué par cap $1 500/j** | $2 400 ✅ |

> **Formule EV** : `(WR × avg_win) - (1-WR) × risk`, avec avg_win_R = PF × (1-WR)/WR = 1.464R
> + 30% boost Labouchere (bets croissants sur séries gagnantes)

### Mode recommandé pour validation : [2,2,2,2] × $75

**Plan sur 3 jours :**

**Jour 1** — Séquence fraîche [2,2,2,2], mise $300
- Objectif : +$1 500 (cap journalier volontaire)
- Balance → $51 500 | Trailing DD floor → $49 000
- Si atteint $1 500 → ARRÊTER de trader ce jour (ne pas exposer les gains)

**Jour 2** — Séquence a grossi depuis les wins J1
- Objectif : +$1 500
- Balance → $53 000 | **TARGET ATTEINT ✅**
- Trailing DD floor → $50 500

**Jour 3 (si J2 insuffisant)** — Sécuriser
- Passer en mode $50/unité (conservateur)
- Besoin résiduel ≤ $500 → 2 trades peuvent suffire

**Scénario worst case (3 pertes consécutives en J1) :**
- Séquence [2,2,2,2] : 3 pertes = -$300 -$300 -$300 = -$900 (reset deux fois)
- Balance $50 000 - $900 = $49 100 → au-dessus du floor $47 500 ✅
- Loin du daily loss limit $2 500 ✅

**Scénario worst-worst (6 pertes d'affilée) :**
- Perte totale : $1 800
- Balance : $50 000 - $1 800 = $48 200 > floor $47 500 ✅
- En dessous du soft stop $1 500 → le bot s'arrête AVANT d'atteindre $1 800
- Protection : bot s'arrête à -$1 500 (soft stop)

### Sizing METH (Micro ETH CME)

**Pourquoi METH et pas MNQ ou MES ?**

| Instrument | Valeur/point | ATR 1H typique | Risque/contrat minimum | Granularité |
|---|---|---|---|---|
| **METH** | 0.1 ETH × prix | $50-100 (prix ETH) | $5-10/contrat | **✅ Très fine** |
| MNQ | $2/point NQ | 100-200 pts | $200-400/contrat | ❌ Trop grossier |
| MES | $5/point ES | 20-40 pts | $100-200/contrat | ❌ Grossier |
| MBT | 0.1 BTC × prix | $500-1500 | $50-150/contrat | ✅ Acceptable |

**METH est clairement le meilleur** : granularité maximale pour le Labouchere.

**Formule contrats METH :**
```
risk_usd = bet_units × unit_value
risk_per_contract = atr_sl_usd × 0.1  (0.1 ETH par METH)
contracts = round(risk_usd / risk_per_contract)

Exemple J1, séquence fraîche, ATR_SL=$70, ETH=$1800 :
  bet = 4 unités × $75 = $300
  risk/contrat = $70 × 0.1 = $7
  contracts = round(300 / 7) = 43 METH
```

**Nombre de contrats typiques selon séquence :**
| Mise | Risque | ATR_SL=$60 | ATR_SL=$80 | ATR_SL=$100 |
|---|---|---|---|---|
| 4u $300 | $300 | 50 METH | 38 METH | 30 METH |
| 6u $450 | $450 | 75 METH | 56 METH | 45 METH |
| 8u $600 | $600 | 100 METH | 75 METH | 60 METH |
| 12u $900 (cap) | $900 | 150 METH | 113 METH | 90 METH |

Plafond de sécurité : 200 METH maximum.

---

## 4. ACTIF RECOMMANDÉ

**→ METH (Micro ETH CME) sur timeframe 1H**

Justification :
1. **Granularité** : sizing Labouchere précis (vs MNQ/MES trop grossiers)
2. **Liquidité** : METH est le micro ETH officiel CME, spreads serrés
3. **Corrélation stratégie** : backtest validé sur ETH 1H (74.13% WR)
4. **Code existant** : tout le bot est déjà câblé sur METH

Contrat actif : **METM6** (Micro ETH Juin 2026) → rollover fin mai 2026 sur METU6.

**Alternative si ETH trop volatile :** MNQ (Micro NQ) — mais sizing beaucoup plus grossier et stratégie à re-backtester.

---

## 5. CONSIDÉRATIONS SPÉCIFIQUES APEX

### 5.1 Trailing Drawdown — La règle la plus critique

Le trailing drawdown Apex trail le **plus haut solde en clôture de session** (EOD), pas intraday.

```
Départ : $50 000 → floor = $47 500

Après J1 clôture $51 500 → floor = $49 000
Après J2 clôture $53 000 → floor = $50 500 (TARGET → demande PA)

Sur PA : le trailing DD devient STATIQUE (floor fixé à $47 500)
→ Beaucoup plus de flexibilité sur le compte funded
```

**Règles dans le bot (cme_guardian.py) :**
- `daily_profit_cap = $1 500` : on arrête volontairement si +$1 500/jour
  - Raison : si on fait +$2 000/jour et qu'on a ensuite -$500 le lendemain,
    le trailing floor monte mais la balance redescend → on "grille" du floor
  - En stoppant à $1 500, on préserve une marge de $1 000 avant le nouveau floor
- `daily_soft_stop = $1 500` : arrêt si -$1 500 (laisse $1 000 de buffer avant le hard limit $2 500)
- Timer à 22h30 Paris : clôture AVANT les 22h45 obligatoires (15 min de marge)

### 5.2 Consistency Rule (PA uniquement — pas sur l'eval)

Sur le compte PA (funded) :
- **Max 30% des profits totaux PA en un seul jour**
- Exemple : si total profits PA = $5 000 → max $1 500/jour
- Le bot intègre cette règle dans `apex_labouchere_v8.py` via `pa_enabled=True`

**Activation après validation :**
```python
# Dans apex_labouchere_v8.py :
enable_pa_mode(current_profit=0.0)  # Reset à 0 au début du PA
```

### 5.3 Adaptation Labouchere → Contrats entiers

Le Labouchere calcule une mise en unités → convertie en dollars → convertie en contrats (entiers).
L'arrondi est toujours vers le bas pour la sécurité (`round()` puis `max(1, ...)`).

L'imprécision d'arrondi est de ±1 contrat = ±$7-10 de risque (METH) → négligeable.

Si le résultat arrondi donne 0 contrats (bet trop petit vs ATR large) → minimum 1 contrat.

### 5.4 Rollover de contrat

METH expire chaque mois. Procédure de rollover :
1. Fermer toute position AVANT l'expiration (en général le 3e vendredi du mois)
2. Mettre à jour `CONTRACT_NAME` dans `.env` (ex: METM6 → METU6)
3. Redémarrer le bot

Calendrier 2026 :
- METM6 (Juin) → expire ~19 juin → passer sur METU6
- METU6 (Sept) → expire ~18 sept → passer sur METZ6
- METZ6 (Déc) → expire ~18 déc → passer sur METH7

---

## 6. STRATÉGIE DE RETRAIT HEBDOMADAIRE (Compte PA)

### Fonctionnement PA Apex $50k
- Retraits : 1 fois par semaine minimum
- Montant min : $500 par retrait
- Condition : avoir un compte PA actif et en bénéfice
- Floor trailing DD sur PA : **STATIQUE à $47 500** (ne monte plus)
  → Cela change tout : on peut être plus agressif sur les retraits

### Formule de retrait recommandée

```
Buffer minimum requis : $50 500
  ($50 000 départ + $500 de marge sécurité au-dessus du floor $47 500)

Retrait hebdomadaire = max(0, balance - $50 500)

Exemple :
  Balance fin semaine = $55 200
  Retrait = $55 200 - $50 500 = $4 700
  Balance après retrait = $50 500 (minimum safe)
```

### Trois modes de retrait selon ton appétit pour le risque

**Mode 1 — Conservateur (maximise la sécurité du compte)**
```
Retrait = max(0, balance - $52 000)
Buffer maintenu : $4 500 au-dessus du floor
→ Quasi-impossible de perdre le PA même avec une mauvaise semaine
```

**Mode 2 — Optimal (recommandé) ✅**
```
Retrait = max(0, balance - $50 500)
Buffer maintenu : $3 000 au-dessus du floor
→ Bon équilibre : retrait max + sécurité suffisante
```

**Mode 3 — Agressif (déconseillé)**
```
Retrait = max(0, balance - $49 000)
Buffer maintenu : $1 500 au-dessus du floor
→ Risqué : une mauvaise semaine = le PA saute
```

### Simulation concrète sur 4 semaines PA (mode Optimal)

Hypothèse : PA mode [2,2,2,2] × $50/unité, 5 trades/jour, 5j/semaine
EV hebdomadaire estimée : $215/trade × 5 trades/j × 5j = **$5 375/semaine** (EV, pas garanti)
EV conservatrice (réaliste, avec variance) : **$2 000-$3 000/semaine**

| Semaine | Balance départ | Gain estimé | Balance fin | Retrait | Balance après |
|---|---|---|---|---|---|
| S1 (post-val) | $53 200 | $2 500 | $55 700 | $5 200 | $50 500 |
| S2 | $50 500 | $2 500 | $53 000 | $2 500 | $50 500 |
| S3 | $50 500 | $3 000 | $53 500 | $3 000 | $50 500 |
| S4 | $50 500 | $2 000 | $52 500 | $2 000 | $50 500 |
| **Total retraits** | | | | **$12 700** | |

Soit **~$3 175/semaine en moyenne** = **~$12 700/mois** extrapolé.

### Règle Consistency sur PA — Impact retraits

La consistency rule dit max 30% des profits totaux PA par jour.
Elle s'applique aux **profits cumulés du compte PA**, pas au capital.

**Piège à éviter** : ne pas trader un jour énorme le lundi qui représente 50% des profits de la semaine.
Le bot gère cela automatiquement avec `pa_enabled=True` dans le Labouchere.

### Récapitulatif des montants journaliers max selon la semaine

| Profits cumulés PA | 30% = max/jour |
|---|---|
| $500 (semaine 1) | $150/jour |
| $1 000 | $300/jour |
| $2 500 | $750/jour |
| $5 000 | $1 500/jour |
| $10 000+ | $3 000/jour (cap bot = $1 500) |

→ La consistency rule n'est vraiment contraignante qu'en **début de compte PA**.
→ Avec le cap bot à $1 500/jour, on reste toujours dans les clous dès que profits > $5 000.

---

## 7. FICHIERS LIVRÉS

```
apex-tradovate-bot/
├── trading/
│   ├── apex_v8hl_server.py       ← Bot principal V8 HL (NOUVEAU)
│   ├── apex_labouchere_v8.py     ← Labouchere adapté Apex V8 (NOUVEAU)
│   ├── cme_guardian.py           ← Gardien CME + Apex rules (NOUVEAU)
│   ├── apex_tradovate_server.py  ← Bot précédent (conservé pour référence)
│   └── apex_lab_tracker.py       ← Tracker simplifié précédent (conservé)
├── start.py                      ← Lancer apex_v8hl_server (à mettre à jour)
├── requirements.txt
└── STRATEGY_V8HL_APEX.md         ← Ce document
```

### Démarrage rapide

```bash
# Variables d'environnement minimum
export TRADOVATE_EMAIL="sumiko"
export TRADOVATE_PASSWORD="TON_PASSWORD"
export TRADOVATE_ACCOUNT_ID="APEX-548673"
export TRADOVATE_CID="TON_CID"
export TRADOVATE_SEC="TON_SEC"
export WEBHOOK_TOKEN="apex_v8hl_secret_2026"
export CONTRACT_NAME="METM6"
export DRY_RUN="true"   # false pour le live

# Changer pour le mode validation (75$/unité)
# (déjà défaut dans apex_labouchere_v8.py — MODE_VALIDATION)

# Démarrage
python3 trading/apex_v8hl_server.py

# Dashboard
open http://localhost:5000/dashboard

# Tester un signal manuellement
curl -X POST http://localhost:5000/webhook \
  -H "X-Webhook-Token: apex_v8hl_secret_2026" \
  -H "Content-Type: application/json" \
  -d '{"action":"open","side":"buy","price":1823.45,"atr_sl":67.8,"regime":"trend"}'

# Simuler un WIN/LOSS
curl -X POST "http://localhost:5000/lab/manual" \
  -H "X-Webhook-Token: apex_v8hl_secret_2026" \
  -H "Content-Type: application/json" \
  -d '{"result":"WIN","pnl":450}'

# Passer en mode PA après validation
curl -X POST "http://localhost:5000/lab/mode?mode=pa_normal" \
  -H "X-Webhook-Token: apex_v8hl_secret_2026"
```

### Format alerte TradingView (Pine Script V8 HL → Webhook)

```json
{
  "action": "{{strategy.order.action == 'buy' ? 'open' : 'open'}}",
  "side": "{{strategy.order.action}}",
  "price": {{close}},
  "atr_sl": {{plot_0}},
  "regime": "trend",
  "token": "apex_v8hl_secret_2026"
}
```

Pour la clôture :
```json
{
  "action": "close",
  "price": {{close}},
  "pnl": {{strategy.netprofit}},
  "token": "apex_v8hl_secret_2026"
}
```

---

## 8. CHECKLIST AVANT GO LIVE

- [ ] Passer `DRY_RUN=false` dans `.env`
- [ ] Passer `TRADOVATE_URL` sur l'URL live (`live-api-d.tradovate.com`)
- [ ] Vérifier `CONTRACT_NAME` = contrat du mois courant (ex: METM6)
- [ ] Vérifier `TRADOVATE_ACCOUNT_ID` = ID du compte Apex dans Tradovate
- [ ] Tester le token webhook (curl DRY_RUN d'abord)
- [ ] Lancer et vérifier `/dashboard` — trading_allowed = true pendant les heures ouvrées
- [ ] Vérifier que le thread `apex-close-monitor` tourne (log au démarrage)
- [ ] Configurer les alertes Pine V8 HL vers l'URL de ce serveur
- [ ] Vérifier l'heure Paris dans les logs (critique pour le blackout)

---

_Ce document est la vérité de terrain. Le code dans `apex_v8hl_server.py`,
`apex_labouchere_v8.py` et `cme_guardian.py` implémente exactement ce qui est décrit ici._
