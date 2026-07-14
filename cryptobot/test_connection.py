import asyncio
import ccxt.async_support as ccxt_async
from config import settings

async def main():
    print(f"Connecting to {settings.exchange_id} (demo mode), symbol={settings.symbol}\n")
    exchange = ccxt_async.bybit({
        "apiKey": settings.api_key,
        "secret": settings.api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"},
    })
    exchange.enable_demo_trading(True)
    try:
        print("[1/3] Public call: fetch_time() ...")
        server_time = await exchange.fetch_time()
        print(f"      OK - server time: {server_time}\n")
        print("[2/3] Private call: fetch_balance() ...")
        balance = await exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        print(f"      OK - USDT total: {usdt.get('total')}, free: {usdt.get('free')}\n")
        print("[3/3] Market check: fetch_ticker() ...")
        ticker = await exchange.fetch_ticker(settings.symbol)
        print(f"      OK - {settings.symbol} last price: {ticker.get('last')}\n")
        print("All checks passed - your connection is ready.")
    except Exception as e:
        print(f"FAILED: {e}")
    finally:
        await exchange.close()

asyncio.run(main())
