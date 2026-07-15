with open('data_manager.py', 'r') as f:
    content = f.read()

old = '''"enableRateLimit": True, "urls": {"api": {"rest": "https://api-demo.bybit.com"}},
            # NOTE: 'future' works for Binance USDM (binanceusdm). Other
            # exchanges use different conventions (e.g. Bybit's unified
            # API generally wants 'linear'/'swap') - check ccxt's docs for
            # your specific exchange_id if you switch.
            "options": {"defaultType": "linear"},
        })
        if self.settings.testnet:
            try:
                exchange.enable_demo_trading(True)
            except Exception as e:  # noqa: BLE001 - sandbox support varies by exchange
                self.log.warning(
                    "set_sandbox_mode not supported/failed for %s: %s", self.settings.exchange_id, e
                )'''

new = '''"enableRateLimit": True,
            "options": {"defaultType": "linear"},
        })
        exchange.enable_demo_trading(True)'''

content = content.replace(old, new)
with open('data_manager.py', 'w') as f:
    f.write(content)
print("Done")
