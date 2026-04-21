#!/usr/bin/env python3
"""
refresh_token.py — Renouvelle le token Tradovate et le pousse sur Railway.
Lancé automatiquement toutes les 90 min via cron OpenClaw.
"""
import asyncio, json, os, sys, subprocess
from playwright.async_api import async_playwright

RAILWAY_TOKEN = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Railway-API-Token", "-w"],
    text=True
).strip()

TRADOVATE_PASSWORD = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Tradovate-Personal-Password", "-w"],
    text=True
).strip()

SERVICE_ID = "58bf4592-c1c2-468a-8da1-e84ca8b84183"
ENV_ID     = "b50fe3f2-cf43-43ab-b7b1-3a57f3d5f739"
PROJECT_ID = "1f89c3f7-5c90-42a0-a512-1e393f61cda8"
BOT_URL    = "https://apex-tradovate-bot-production-d7c7.up.railway.app"

async def get_token():
    access_token = None
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def on_response(response):
            nonlocal access_token
            if "tradovateapi.com" in response.url and "accesstoken" in response.url.lower():
                try:
                    body = await response.json()
                    if "accessToken" in body:
                        access_token = body["accessToken"]
                except:
                    pass

        page.on("response", on_response)
        await page.goto("https://trader.tradovate.com", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        await page.fill("input[type='text']", "sumiko")
        await page.fill("input[type='password']", TRADOVATE_PASSWORD)
        await page.click("button:has-text('Login')")
        await asyncio.sleep(10)
        if "current-sessions" in page.url:
            await page.click("button:has-text('Fermez la session')")
            await asyncio.sleep(6)
        await browser.close()
    return access_token

def push_to_railway(token: str):
    import urllib.request
    query = "mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }"
    variables = {
        "input": {
            "projectId": PROJECT_ID,
            "environmentId": ENV_ID,
            "serviceId": SERVICE_ID,
            "variables": {"TRADOVATE_ACCESS_TOKEN": token}
        }
    }
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://backboard.railway.app/graphql/v2",
        data=body,
        headers={
            "Authorization": f"Bearer {RAILWAY_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "apex-bot-refresh/1.0"
        }
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def notify_bot(token: str):
    """Notifie le bot Railway du nouveau token via endpoint dédié."""
    import urllib.request
    WEBHOOK_TOKEN = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Apex-Webhook-Token", "-w"],
        text=True
    ).strip()
    body = json.dumps({"access_token": token}).encode()
    req = urllib.request.Request(
        f"{BOT_URL}/refresh_token",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Token": WEBHOOK_TOKEN
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Endpoint refresh_token non disponible: {e}")
        return None

if __name__ == "__main__":
    print("Récupération du token Tradovate...")
    token = asyncio.run(get_token())
    if not token:
        print("ERREUR: Token non obtenu")
        sys.exit(1)
    print(f"Token OK: {token[:30]}...")

    print("Push vers Railway...")
    result = push_to_railway(token)
    print(f"Railway: {result}")

    print("Notification du bot...")
    notify_bot(token)

    print("Token rafraîchi avec succès ✅")
