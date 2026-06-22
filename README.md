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
