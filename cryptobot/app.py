import os
import threading
import asyncio
import logging
from fastapi import FastAPI
import uvicorn

# Configure logging to output directly to Hugging Face logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("HF_KeepAlive")

app = FastAPI(title="CCXT Futures Trading Bot Keep-Alive")

@app.get("/")
def read_root():
    return {
        "status": "online",
        "bot": "15m Multi-Indicator Futures Trading Bot",
        "hosting": "Hugging Face Spaces (Docker)",
        "persistence": "SQLite (Ephemeral)"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}

def run_trading_bot():
    logger.info("Initializing trading bot background thread...")
    try:
        # Import the entry point from your existing main.py
        import main
        
        # Determine if your execution entry point is 'main' or 'main_loop'
        # and safely execute it whether it is written as sync or async.
        entry_func = None
        if hasattr(main, "main"):
            entry_func = main.main
        elif hasattr(main, "main_loop"):
            entry_func = main.main_loop
            
        if entry_func is None:
            logger.critical("Could not find 'main()' or 'main_loop()' in main.py. Check entry point naming.")
            return

        if asyncio.iscoroutinefunction(entry_func):
            logger.info("Asynchronous entry point detected. Running event loop...")
            asyncio.run(entry_func())
        else:
            logger.info("Synchronous entry point detected. Running...")
            entry_func()
            
    except Exception as e:
        logger.critical(f"Critical failure in trading bot thread execution: {e}", exc_info=True)

if __name__ == "__main__":
    # Start the bot thread as a background daemon so it doesn't block the web server
    bot_thread = threading.Thread(target=run_trading_bot, name="BotEngineThread", daemon=True)
    bot_thread.start()
    
    # Run the web server to handle Hugging Face keepalive traffic
    port = int(os.environ.get("PORT", 7860))
    logger.info(f"Starting FastAPI webserver on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
