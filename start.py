#!/usr/bin/env python3
"""
Apex Tradovate Bot — TradingView webhook -> Tradovate MNQ (Micro NQ Futures)
Regles Apex Trader Funding :
  - Session US uniquement 09h30-16h00 ET
  - Cloture forcee a 15h55 ET (5 min avant fermeture)
  - Labouchere [50,50,50,50] en dollars
  - Capital $50 000 evaluation
"""
import logging
import os
import sys
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("start")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading"))

log.info("Apex Tradovate Bot MNQ - demarrage")
log.info("Strategie : Range Bar HMA 9R | Labouchere [50,50,50,50] | Session US 09h30-16h ET")

port = int(os.environ.get("PORT", 8080))

from trading.apex_tradovate_bot import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
