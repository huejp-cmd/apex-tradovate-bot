#!/usr/bin/env python3
"""
Apex Tradovate Bot — TradingView webhook -> Tradovate MNQ
Labouchere [50,50,50,50] | Session US 09h30-16h ET | Apex $50k eval
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

# Ajouter trading/ au path pour que les imports relatifs fonctionnent
trading_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading")
sys.path.insert(0, trading_dir)

log = logging.getLogger("start")
log.info("Apex Tradovate Bot MNQ - demarrage")
log.info("Strategie : NQ Range Bar 9R HMA | Labouchere [50,50,50,50] | Session 09h30-16h ET")

# Import apres ajout du path
from apex_tradovate_bot import app  # noqa: E402

port = int(os.environ.get("PORT", 8080))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
