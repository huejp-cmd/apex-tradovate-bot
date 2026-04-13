#!/usr/bin/env python3
"""
Apex Tradovate Bot — TradingView → Tradovate (CME Micro ETH Futures)
Règles Apex Trader Funding :
  - Clôture obligatoire avant 22h45 Paris (16h45 EDT)
  - Blackout overnight 22h45 → 00h00 Paris
  - Blackout weekend vendredi 22h45 → lundi 00h00
"""
import logging, sys, os
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("start")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading"))
log.info("🚀 Apex Tradovate Bot — démarrage")
log.info("📡 Stratégie : SOL29 v6 ETH 45M — Apex $50k")
import apex_tradovate_server
port = int(os.environ.get("PORT", 8080))
apex_tradovate_server.app.run(host="0.0.0.0", port=port, debug=False)
