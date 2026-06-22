# Binance Futures Trading Engine

A config-driven Binance Futures trading engine for systematic trade execution, position management, persistent state tracking, risk controls, and optional Telegram monitoring.

## Overview

This project is a Python-based trading engine built around rule-based execution logic rather than manual trading decisions.

The engine uses a JSON configuration file to control symbols, position settings, capital allocation, entry logic, scaling rules, take-profit logic, cooldowns, and optional monitoring features.

It was designed as a practical automation project for managing Binance Futures positions with persistent local state and exchange-aware order execution.

## Features

- Binance Futures integration
- Config-driven execution logic
- Multi-symbol support
- Hedge mode support
- Cross/isolated margin configuration
- Position scaling logic
- Persistent local state tracking
- Take-profit management
- Entry and scaling cooldowns
- Exchange rule handling for quantity precision
- Optional Telegram notifications
- Logging to console and file

## Project Structure

```text
.
├── trader.py
├── config.example.json
├── requirements.txt
├── .gitignore
└── README.md
Setup

Install dependencies:

pip install -r requirements.txt

Create your private config file:

cp config.example.json config.json

Edit config.json and add your own Binance API credentials.

Run the engine:

python trader.py
Configuration

The engine is controlled through config.json.

The repository includes config.example.json as a safe template. Real API keys, Telegram tokens, logs, state files, and local configuration files should not be committed to the repository.

Security Notes

Never commit:

config.json
API keys
Telegram bot tokens
log files
state files
exchange account data

Use config.example.json only as a public template.

Disclaimer: This project is for educational and portfolio purposes. It is not financial advice. Automated trading involves risk, including the risk of financial loss. Use at your own responsibility.
