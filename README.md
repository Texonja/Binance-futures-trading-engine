# Binance Futures Trading Engine

Current release [Bot2027 v4.0.0](https://github.com/Texonja/Binance-futures-trading-engine/releases/tag/v4.0.0).

A config-driven Binance Futures trading engine for systematic trade execution, position management, persistent state tracking, risk controls, and optional Telegram monitoring.

## Overview

This project is a Python-based trading engine built around rule-based execution logic rather than manual trading decisions.

The engine uses a JSON configuration file to control symbols, position settings, capital allocation, entry logic, scaling rules, take-profit logic, cooldowns, and optional monitoring features.

It was designed as a practical automation project for managing Binance Futures positions with persistent local state and exchange-aware order execution.

## Features

* Binance Futures integration
* Config-driven execution logic
* Multi-symbol support
* Long-side trading in Hedge Mode with 1× leverage and cross margin
* Position scaling logic
* Persistent local state tracking
* Take-profit management
* Entry and scaling cooldowns
* Exchange rule handling for quantity precision
* Optional Telegram notifications
* Logging to console and file
* Reconciliation of pending orders after a restart

## Project Files

* `trader.py` - main trading engine
* `config.example.json` - safe example configuration
* `requirements.txt` - Python dependencies
* `.gitignore` - ignored local/private files
* `README.md` - project documentation

## Setup

Install dependencies:

`pip install -r requirements.txt`

For a fresh installation, copy `config.example.json` to `config.json` in the same directory as `trader.py`. Set the Binance API credentials and review the example symbol and order sizes before starting the engine.

For an existing installation, stop the running bot and back up `config.json` and `bot_state.json` before updating `trader.py`. Start the new version from the directory containing those files so it can recover the recorded levels and any pending order.

Run the engine:

`python trader.py`

## Configuration

The engine is controlled through `config.json`.

Set `price.lookback` to the number of completed one-minute candles used for the entry signal. Each DCA level needs one entry in `capital.drop` and `capital.risk`. The `capital.profit` list includes one additional entry for the initial position. For per-level cooldowns, supply one value in minutes per DCA level in `order.open.cooldown`, or use an empty list to disable them.

Keys such as `trailing`, `compound` and `slippage` are reserved for future versions and currently have no effect.

The repository includes `config.example.json` as a safe public template. Real API keys, Telegram tokens, logs, state files, and local configuration files should not be committed to the repository.

## Security Notes

Never commit:

* `config.json`
* API keys
* Telegram bot tokens
* log files
* state files
* exchange account data

Use `config.example.json` only as a public template.

## Disclaimer

This project is for educational and portfolio purposes.

It is not financial advice. Automated trading involves risk, including the risk of financial loss. Use at your own responsibility.
