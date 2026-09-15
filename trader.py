import json
import time
import logging
import requests
import os
import sys
import uuid
import math
import copy
import re
from email.utils import parsedate_to_datetime
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ======================================================================================
# PUTANJE I LOGOVANJE - Bot2027 v2
# ======================================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BOT_LOG_FILE = os.path.join(BASE_DIR, "bot_log.txt")
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
AUDIT_FILE = os.path.join(BASE_DIR, "bot_audit.jsonl")
INSTANCE_LOCK_FILE = os.path.join(BASE_DIR, "bot2027.lock")

class ColorFormatter(logging.Formatter):
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"

    def format(self, record):
        text = super().format(record)
        if record.levelno >= logging.ERROR:
            return f"{self.RED}{text}{self.RESET}"
        if record.levelno >= logging.WARNING or getattr(record, "yellow", False):
            return f"{self.YELLOW}{text}{self.RESET}"
        return text


logger = logging.getLogger("MultiBot")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.handlers.clear()

_plain_fmt = logging.Formatter(
    '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

_file_handler = logging.FileHandler(BOT_LOG_FILE, mode='a', encoding='utf-8')
_file_handler.setFormatter(_plain_fmt)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(ColorFormatter(
    '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(_console_handler)

# Sentinel za update_state: razlikuje "ne menjaj" od eksplicitnog None.
_UNCHANGED = object()


class ApiBackoffError(RuntimeError):
    """Lokalna pauza API zahteva; nije odgovor o ishodu naloga."""


class StateLoadError(RuntimeError):
    """State fajl postoji, ali ne moze bezbedno da se ucita."""


class SingleInstanceLock:
    """OS-level lock: sprecava da dve instance Bot2027 v2 rade istovremeno."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        try:
            if not os.path.exists(self.path):
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(" ")

            self.handle = open(self.path, "r+", encoding="utf-8")
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(" ")
                self.handle.flush()
            self.handle.seek(0)

            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(f"pid={os.getpid()} started={datetime.now().astimezone().isoformat()}\n")
            self.handle.flush()
            return True
        except (OSError, IOError):
            if self.handle is not None:
                try:
                    self.handle.close()
                except Exception:
                    pass
                self.handle = None
            return False

    def release(self):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            try:
                self.handle.close()
            finally:
                self.handle = None

# ======================================================================================
# TELEGRAM MODUL
# ======================================================================================
class TelegramBot:
    def __init__(self, token, chat_id, enabled=True):
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, message, critical=False):
        if not self.enabled:
            return True
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            response = requests.post(self.base_url, data=payload, timeout=5)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok", False):
                raise RuntimeError(f"Telegram API ok=false: {data}")
            return True
        except Exception as e:
            log_fn = logger.error if critical else logger.warning
            error_text = str(e).replace(self.token, '<redacted>') if self.token else str(e)
            log_fn(f"Telegram poruka nije poslata: {error_text}")
            return False

# ======================================================================================
# KLASA ZA KONFIGURACIJU
# ======================================================================================
class Config:
    def __init__(self, filename=None):
        if filename is None:
            filename = CONFIG_FILE
        elif not os.path.isabs(filename):
            filename = os.path.join(BASE_DIR, filename)

        self.filename = os.path.abspath(filename)
        with open(self.filename, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        
        self.api_key = self.data['key']['public']
        self.api_secret = self.data['key']['private']
        self.symbols = self.data['symbol'] 
        self.leverage = int(self.data['position']['leverage'])
        self.hedge_mode = self.data['position']['hedge']
        
        pos_data = self.data.get('position', {})
        raw_type = pos_data.get('type', 'ONE_WAY')
        self.margin_type = "CROSSED" if raw_type == "CROSS" else raw_type
        
        self.tg_enabled = bool(self.data['extra']['telegram'])
        self.tg_chat_id = self.data['extra']['telegramChat']
        self.tg_token = self.data['extra'].get('telegramToken') or os.environ.get('BOT2027_TELEGRAM_TOKEN', '')
        
        self.fng_enabled = self.data['extra'].get('fng_enabled', False)
        self.fng_max = self.data['extra'].get('fng_buy_max', 30)
        
        self.base_order_size = float(self.data['capital']['min']) 
        self.drops = self.data['capital']['drop'] 
        self.risks = self.data['capital']['risk'] 
        self.profits = self.data['capital']['profit'] 
        
        self.lookback = int(self.data['price']['lookback'])
        
        self.entry_cooldown_min = self.data['order'].get('entryCooldown', 0)
        self.dca_cooldowns_min = self.data['order']['open'].get('cooldown', [])

        extra = self.data.get('extra', {})
        raw_alert_days = float(extra.get('forceCloseDays', 120) or 120)
        self.position_age_alert_days = raw_alert_days if raw_alert_days > 0 else 120.0
        self.position_age_alert_every_sec = float(extra.get('forceAlertEverySec', 3600) or 3600)
        self.symbol_sleep_sec = 0.5
        self.validate()

    def validate(self):
        """Provera formata i dogovorenog LONG / x1 / CROSS setup-a pre Client-a."""
        if self.data['position']['leverage'] not in (1, '1') or isinstance(self.data['position']['leverage'], bool):
            raise ValueError('Tenk zahteva leverage=1')
        if self.hedge_mode is not True or self.margin_type != 'CROSSED':
            raise ValueError('Tenk zahteva position.hedge=true i position.type=CROSS/CROSSED')
        if not isinstance(self.symbols, list) or not self.symbols or any(not isinstance(s, str) or not s for s in self.symbols):
            raise ValueError('symbol mora biti neprazna lista simbola')
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError('symbol sadrzi duplikate')
        if not math.isfinite(self.base_order_size) or self.base_order_size <= 0:
            raise ValueError('capital.min mora biti pozitivan konacan broj')
        for name, values in (('drop', self.drops), ('risk', self.risks), ('profit', self.profits)):
            if not isinstance(values, list) or not values or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError(f'capital.{name} mora biti lista pozitivnih konacnih brojeva')
        if len(self.risks) != len(self.drops) or len(self.profits) != len(self.drops) + 1:
            raise ValueError('risk mora imati po jednu stavku za svaki DCA, profit i dodatnu stavku za P1')
        if any(d >= 100 for d in self.drops) or any(a >= b for a, b in zip(self.drops, self.drops[1:])):
            raise ValueError('drop mora strogo rasti i biti manji od 100%')
        if type(self.data['price']['lookback']) is not int or not 1 <= self.lookback <= 1499:
            raise ValueError('price.lookback mora biti ceo broj 1..1499')
        if not isinstance(self.dca_cooldowns_min, list) or len(self.dca_cooldowns_min) not in (0, len(self.drops)):
            raise ValueError('DCA cooldown lista mora biti prazna ili imati stavku za svaki DCA')
        timings = [self.entry_cooldown_min, self.position_age_alert_days, self.position_age_alert_every_sec] + self.dca_cooldowns_min
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in timings):
            raise ValueError('Vremenski parametri moraju biti konacni nenegativni brojevi')
        if self.tg_enabled and (not self.tg_token or not self.tg_chat_id):
            raise ValueError('Telegram je ukljucen: upisi extra.telegramToken (ili BOT2027_TELEGRAM_TOKEN) i extra.telegramChat')

# ======================================================================================
# GLAVNA KLASA BOTA
# ======================================================================================
class MultiCoinBot:
    def __init__(self):
        self.cfg = Config()
        self._api_retry = {}
        self._api_global_until = 0.0
        self._buy_retry_until = {}
        self.client = Client(self.cfg.api_key, self.cfg.api_secret)
        self.tg = TelegramBot(self.cfg.tg_token, self.cfg.tg_chat_id, self.cfg.tg_enabled)
        self.rules = {} 
        
        self.fng_value = 50
        self.fng_last_update = 0
        
        self.last_trade_time = {} 
        self.last_close_time = {} 

        # --- STATE LOCK SYSTEM ---
        self.state_file = STATE_FILE
        self.audit_file = AUDIT_FILE
        self.state_reload_healthy = True
        self.locked_levels = self.load_state(allow_missing=True)
        self._mismatch_warn_cache = {}
        self._warning_cache = {}
        self._error_cache = {}
        self._pending_warn_cache = {}
        self._pending_not_found_cache = {}
        self._age_alert_cache = {}
        self._open_time_attempt_cache = {}
        self.disabled_symbols = set()

        logger.info(f"CONFIG_FILE: {self.cfg.filename}")
        logger.info(f"STATE_FILE: {self.state_file}")
        logger.info(f"AUDIT_FILE: {self.audit_file}")
        logger.info(f"EFFECTIVE_CONFIG: symbols={self.cfg.symbols} base_order={self.cfg.base_order_size}")
        for _s, _v in self.locked_levels.items():
            if isinstance(_v, dict):
                logger.info(
                    f"STATE_LOAD {_s}: level={_v.get('level')} base={_v.get('base_price')} "
                    f"open_time={_v.get('open_time')} pending={bool(_v.get('pending_order'))}"
                )

    # --- STATE / AUDIT ---
    def _default_state(self):
        return {"level": 0, "base_price": None, "open_time": None, "pending_order": None}

    def audit(self, event, symbol=None, severity="INFO", **fields):
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            "severity": severity
        }
        if symbol is not None:
            row["symbol"] = symbol
        row.update(fields)
        try:
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as e:
            logger.warning(f"AUDIT log nije mogao da se upise: {e}")

    def warn_rate_limited(self, key, message, every_sec=300):
        now = time.time()
        prev = self._warning_cache.get(key, 0)
        if now - prev >= every_sec:
            logger.warning(message)
            self._warning_cache[key] = now

    def error_rate_limited(self, key, message, every_sec=60):
        now = time.time()
        prev = self._error_cache.get(key, 0)
        if now - prev >= every_sec:
            logger.error(message)
            self._error_cache[key] = now

    def load_state(self, allow_missing=False):
        """Validan STATE je zakon. Proverava format, nikad ne rekonstruise nivo po kolicini."""
        if not os.path.exists(self.state_file):
            if allow_missing:
                return {}
            raise StateLoadError(f'State fajl ne postoji: {self.state_file}')
        try:
            with open(self.state_file, 'r', encoding='utf-8-sig') as f:
                raw_data = json.load(f)
            if not isinstance(raw_data, dict):
                raise ValueError('top-level state mora biti JSON object/dict')
            new_data = {}
            for sym, val in raw_data.items():
                if not isinstance(sym, str) or not sym:
                    raise ValueError('Nevalidan simbol u STATE-u')
                st = self._default_state()
                if type(val) is int:
                    st['level'] = val
                elif isinstance(val, dict) and 'level' in val:
                    st.update(val)  # Sacuvaj i korisnicka dodatna polja.
                    st['open_time'] = val.get('open_time', val.get('openTime'))
                else:
                    raise ValueError(f'Nevalidan state zapis za {sym}')
                self._validate_level(st['level'])
                for field in ('base_price', 'open_time'):
                    if st[field] is not None:
                        st[field] = self._number(st[field], f'{sym}.{field}')
                self._validate_pending(st['pending_order'])
                new_data[sym] = st
            return new_data
        except Exception as e:
            raise StateLoadError(f'CRITICAL STATE LOAD ERROR: {self.state_file}: {e}') from e

    def save_state(self):
        """Snima state kao i stara verzija; bez atomic/backup mehanizma po dogovoru."""
        if not self.state_reload_healthy:
            logger.error("CRITICAL STATE SAVE BLOCKED: poslednji reload state-a nije bio uspesan.")
            return False
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.locked_levels, f, indent=4)
            return True
        except Exception as e:
            logger.error(f"CRITICAL STATE SAVE ERROR: {e}")
            self.audit("STATE_SAVE_ERROR", severity="ERROR", error=str(e))
            return False

    def reload_state(self):
        """Manual edit reload. Na gresci cuva poslednji zdrav RAM state i blokira trading."""
        try:
            if not os.path.exists(self.state_file):
                if self.locked_levels:
                    raise StateLoadError(f"State fajl je nestao: {self.state_file}")
                if not self.state_reload_healthy:
                    logger.info("STATE RELOAD RECOVERED: state fajl nije potreban jer je state prazan.")
                self.state_reload_healthy = True
                return True

            new_state = self.load_state(allow_missing=False)
            was_unhealthy = not self.state_reload_healthy
            self.locked_levels = new_state
            self.state_reload_healthy = True
            if was_unhealthy:
                logger.info("STATE RELOAD RECOVERED: trading state je ponovo citljiv.")
                self.audit("STATE_RELOAD_RECOVERED")
            return True
        except Exception as e:
            self.state_reload_healthy = False
            self.error_rate_limited(
                "state_reload_failed",
                f"CRITICAL STATE RELOAD ERROR: {e}. Poslednji zdrav RAM state je sacuvan; NOVI ORDERI SU BLOKIRANI.",
                every_sec=60
            )
            self.audit("STATE_RELOAD_ERROR", severity="ERROR", error=str(e))
            return False

    def update_state(self, symbol, level=None, base_price=_UNCHANGED, open_time=_UNCHANGED,
                     pending_order=_UNCHANGED, expected_state=None):
        """Ucitaj rucne izmene pre upisa; zastarela odluka ne sme pregaziti nov STATE."""
        if not self.state_reload_healthy or not self.reload_state():
            return False
        old_state = copy.deepcopy(self.locked_levels.get(symbol, self._default_state()))
        if expected_state is not None and old_state != expected_state:
            logger.warning(f'{symbol}: STATE promenjen tokom obrade; stara odluka nije upisana.')
            return False
        st = copy.deepcopy(old_state)
        if level is not None:
            st['level'] = self._validate_level(level)
        if base_price is not _UNCHANGED:
            st['base_price'] = base_price
        if open_time is not _UNCHANGED:
            st['open_time'] = open_time
        if pending_order is not _UNCHANGED:
            self._validate_pending(pending_order)
            st['pending_order'] = pending_order
        self.locked_levels[symbol] = st
        if self.save_state():
            self.audit('STATE_UPDATE', symbol=symbol, level=st.get('level'), base_price=st.get('base_price'),
                       open_time=st.get('open_time'), pending=st.get('pending_order') is not None)
            return True
        self.locked_levels[symbol] = old_state
        # Ne dozvoli da naredni save pregazi potencijalno ostecen fajl bez zdravog reload-a.
        self.state_reload_healthy = False
        return False

    # -----------------------------------------------

    def setup(self):
        """Inicijalizacija Bot2027 v2 + verifikacija leverage/margin/position mode stanja."""
        start_msg = (
            f"🚀 <b>Bot2027 v2 STARTED</b>\n"
            f"Simboli: {self.cfg.symbols}\n"
            f"Leverage: x{self.cfg.leverage} | Margin: {self.cfg.margin_type}\n"
            f"120d alert: {self.cfg.position_age_alert_days:g}d | Sleep: {self.cfg.symbol_sleep_sec:g}s"
        )
        logger.info(start_msg.replace("<b>", "").replace("</b>", "").replace("\n", " | "))
        self.tg.send(start_msg)

        try:
            logger.info("Preuzimam pravila sa berze...")
            exchange_info = self._api_call('futures_exchange_info')

            if not self.verify_position_mode():
                return False

            for target_symbol in self.cfg.symbols:
                self.last_trade_time[target_symbol] = 0
                self.last_close_time[target_symbol] = 0

                try:
                    self._api_call('futures_change_leverage',
                        symbol=target_symbol, leverage=self.cfg.leverage
                    )
                except BinanceAPIException as e:
                    logger.warning(
                        f"{target_symbol}: change_leverage odgovor: code={getattr(e, 'code', None)} {e}"
                    )

                try:
                    self._api_call('futures_change_margin_type',
                        symbol=target_symbol, marginType=self.cfg.margin_type
                    )
                except BinanceAPIException as e:
                    # -4046 je tipicno 'No need to change margin type'. Verifikacija ispod je autoritativna.
                    if getattr(e, 'code', None) == -4046:
                        logger.info(f"{target_symbol}: margin type je vec podesen.")
                    else:
                        logger.warning(
                            f"{target_symbol}: change_margin_type odgovor: code={getattr(e, 'code', None)} {e}"
                        )

                found_rule = False
                for s_info in exchange_info['symbols']:
                    if s_info['symbol'] == target_symbol:
                        step_size = float([
                            f['stepSize'] for f in s_info['filters']
                            if f['filterType'] == 'LOT_SIZE'
                        ][0])
                        self.rules[target_symbol] = {
                            'qty_prec': int(s_info['quantityPrecision']),
                            'step_size': step_size
                        }
                        found_rule = True
                        break

                if not found_rule:
                    logger.error(f"CRITICAL {target_symbol}: nema exchange pravila; trading disabled.")
                    self.disabled_symbols.add(target_symbol)
                    self.audit("SYMBOL_DISABLED", target_symbol, severity="ERROR", reason="exchange_rules_missing")
                    continue

                self.verify_leverage_margin(target_symbol)
                if target_symbol not in self.disabled_symbols:
                    logger.info(f"✅ {target_symbol}: Ready")

            return True
        except Exception as e:
            logger.error(f"CRITICAL SETUP ERROR: {e}")
            self.audit("SETUP_ERROR", severity="ERROR", error=str(e))
            return False

    @staticmethod
    def _as_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() == "true"

    def verify_position_mode(self):
        """Account-wide provera: config Hedge/One-way mora odgovarati Binance stanju."""
        try:
            mode = self._api_call('futures_get_position_mode')
            if not isinstance(mode, dict) or "dualSidePosition" not in mode:
                raise RuntimeError("Binance position-mode response nema dualSidePosition")

            actual_hedge = self._as_bool(mode.get("dualSidePosition"))
            if actual_hedge != bool(self.cfg.hedge_mode):
                raise RuntimeError(
                    f"position mode mismatch expected_hedge={self.cfg.hedge_mode} actual_hedge={actual_hedge}"
                )

            self.audit(
                "POSITION_MODE_VERIFIED",
                expected_hedge=bool(self.cfg.hedge_mode), actual_hedge=actual_hedge
            )
            return True
        except Exception as e:
            logger.error(f"CRITICAL POSITION MODE VERIFICATION FAILED: {e}. BOT NECE TRGOVATI.")
            self.audit("POSITION_MODE_VERIFICATION_FAILED", severity="ERROR", error=str(e))
            return False

    def verify_leverage_margin(self, symbol):
        """Symbol Configuration je autoritet za stvarni leverage i margin type."""
        try:
            if not hasattr(self.client, "futures_symbol_config"):
                raise RuntimeError(
                    "python-binance nema futures_symbol_config(); potreban je noviji python-binance"
                )

            response = self._api_call('futures_symbol_config', symbol=symbol)
            if isinstance(response, dict):
                rows = [response]
            elif isinstance(response, list):
                rows = response
            else:
                raise RuntimeError(f"neocekivan symbolConfig response: {type(response).__name__}")

            row = next((r for r in rows if str(r.get("symbol", "")).upper() == symbol.upper()), None)
            if row is None:
                raise RuntimeError("Binance symbolConfig nije vratio trazeni simbol")
            if "leverage" not in row or "marginType" not in row:
                raise RuntimeError("Binance symbolConfig nema leverage ili marginType")

            actual_lev = int(float(row["leverage"]))
            actual_margin = str(row["marginType"]).upper()
            if actual_margin == "CROSS":
                actual_margin = "CROSSED"

            expected_margin = (
                "CROSSED" if self.cfg.margin_type in ("CROSS", "CROSSED")
                else str(self.cfg.margin_type).upper()
            )

            if actual_lev != self.cfg.leverage:
                raise RuntimeError(
                    f"leverage mismatch expected=x{self.cfg.leverage} actual=x{actual_lev}"
                )
            if actual_margin != expected_margin:
                raise RuntimeError(
                    f"margin mismatch expected={expected_margin} actual={actual_margin}"
                )

            self.audit(
                "CONFIG_VERIFIED", symbol, leverage=actual_lev, margin_type=actual_margin,
                expected_leverage=self.cfg.leverage, expected_margin=expected_margin
            )
            return True
        except Exception as e:
            self.disabled_symbols.add(symbol)
            logger.error(f"CRITICAL {symbol}: {e}. TRADING DISABLED za ovaj simbol.")
            self.audit("SYMBOL_DISABLED", symbol, severity="ERROR", reason=str(e))
            return False

    def get_market_price(self, symbol):
        ticker = self._api_call('futures_symbol_ticker', symbol=symbol)
        if not isinstance(ticker, dict) or ('symbol' in ticker and ticker['symbol'] != symbol):
            raise ValueError('Nevalidan ticker odgovor')
        return self._number(ticker.get('price'), 'ticker.price')
    
    def calculate_base_math(self, symbol, avg_price, position_amt):
        """Stara metoda: Matematicka rekonstrukcija (FALLBACK)."""
        try:
            current_value = abs(position_amt) * avg_price
            capital_data = self.cfg.data['capital']
            min_bet = capital_data['min']
            drops = capital_data['drop']
            risks = capital_data['risk']
            
            if current_value <= min_bet * 1.05: return avg_price

            sim_total_usd = min_bet
            sim_total_coins_factor = min_bet / 1.0 
            
            for i in range(len(drops)):
                if sim_total_usd >= current_value * 0.98: break
                
                drop_pct = drops[i] / 100      
                risk_factor = risks[i]         
                step_usd = min_bet * risk_factor
                step_price_factor = 1.0 - drop_pct 
                step_coins = step_usd / step_price_factor
                
                sim_total_usd += step_usd
                sim_total_coins_factor += step_coins
            
            ratio = sim_total_coins_factor / sim_total_usd
            return avg_price * ratio
        except Exception as e:
            self.warn_rate_limited(
                f"base_math:{symbol}", f"{symbol}: calculate_base_math problem: {e}", 3600
            )
            return None

    def get_real_base_price(self, symbol, avg_price=None, position_amt=None, allow_estimate=False):
        """Za trading vraca iskljucivo sacuvan P1 base; matematika je samo INFO procena."""
        state = self.locked_levels.get(symbol, {})
        stored_base = state.get("base_price")

        try:
            stored_base = float(stored_base)
        except (TypeError, ValueError):
            stored_base = None

        if stored_base is not None and stored_base > 0:
            return stored_base

        if allow_estimate and avg_price is not None and position_amt is not None:
            return self.calculate_base_math(symbol, avg_price, position_amt)
        return None

    def get_valid_position_state(self, symbol):
        if symbol not in self.locked_levels:
            return None, None, 'state zapis za simbol ne postoji'
        try:
            stored = self.locked_levels[symbol]
            level = self._validate_level(stored.get('level'))
            base = self._number(stored.get('base_price'), 'base_price')
            return level, base, None
        except (ValueError, TypeError, AttributeError):
            return None, None, 'nevalidan level ili base_price'

    def get_active_position(self, symbol):
        positions = self._api_call('futures_position_information', symbol=symbol)
        if not isinstance(positions, list):
            raise ValueError('Position odgovor nije lista')
        found = None
        for p in positions:
            if not isinstance(p, dict) or p.get('symbol') != symbol or p.get('positionSide') not in ('LONG', 'SHORT'):
                raise ValueError('Nevalidan symbol/positionSide u Hedge position odgovoru')
            if p['positionSide'] != 'LONG':
                continue
            if found is not None:
                raise ValueError('Dupliran LONG zapis u position odgovoru')
            qty = self._number(p.get('positionAmt'), 'positionAmt', allow_zero=True)
            self._number(p.get('entryPrice'), 'entryPrice', allow_zero=(qty == 0))
            found = p
        return found  # V3 legitimno vraca [] za flat; poznat ciklus se zbog toga ne resetuje.

    def reconstruct_open_time_from_trades(self, symbol, current_position_amt):
        """Pokusaj da rekonstruise vreme P1 iz poslednjih Binance futures tradeova."""
        try:
            trades = self._api_call('futures_account_trades', symbol=symbol, limit=1000)
            if not trades:
                return None

            wanted_side = 'LONG' if self.cfg.hedge_mode else 'BOTH'
            filtered = [
                t for t in trades
                if t.get('positionSide', 'BOTH') == wanted_side
            ]
            filtered.sort(key=lambda t: int(t.get('time', 0)))

            net_qty = 0.0
            cycle_start = None
            for t in filtered:
                qty = abs(float(t.get('qty', 0) or 0))
                side = str(t.get('side', '')).upper()
                ts = float(t.get('time', 0) or 0) / 1000.0

                if side == 'BUY':
                    if net_qty <= 1e-12:
                        cycle_start = ts or None
                    net_qty += qty
                elif side == 'SELL':
                    net_qty = max(0.0, net_qty - qty)
                    if net_qty <= 1e-12:
                        net_qty = 0.0
                        cycle_start = None

            current_qty = abs(float(current_position_amt))
            tolerance = max(1e-8, current_qty * 0.02)
            if cycle_start and abs(net_qty - current_qty) <= tolerance:
                return cycle_start

            return None
        except Exception as e:
            self.warn_rate_limited(
                f"open_time_reconstruct:{symbol}",
                f"{symbol}: open_time rekonstrukcija nije uspela: {e}",
                every_sec=3600
            )
            return None

    def ensure_open_time(self, symbol, pos_data):
        """Vraca pouzdan P1 timestamp; ne koristi position updateTime kao lazni fallback."""
        st = copy.deepcopy(self.locked_levels.get(symbol, {}))
        open_time = st.get('open_time')
        if open_time:
            try:
                return float(open_time)
            except Exception:
                pass

        if not pos_data:
            return None

        now = time.time()
        last_attempt = self._open_time_attempt_cache.get(symbol, 0)
        if now - last_attempt < 3600:
            return None
        self._open_time_attempt_cache[symbol] = now

        current_amt = float(pos_data.get('positionAmt', 0) or 0)
        reconstructed = self.reconstruct_open_time_from_trades(symbol, current_amt)
        if reconstructed:
            if self.update_state(symbol, open_time=reconstructed, expected_state=st):
                logger.info(
                    f"{symbol}: open_time rekonstruisan iz Binance trade history: "
                    f"{datetime.fromtimestamp(reconstructed).isoformat(sep=' ', timespec='seconds')}"
                )
                self.audit("OPEN_TIME_RECONSTRUCTED", symbol, open_time=reconstructed)
                return reconstructed

        self.warn_rate_limited(
            f"open_time_unknown:{symbol}",
            f"{symbol}: OPEN_TIME UNKNOWN za postojecu poziciju; 120d alert nije pouzdan dok se vreme P1 ne utvrdi.",
            every_sec=3600
        )
        self.audit("OPEN_TIME_UNKNOWN", symbol, severity="WARNING")
        return None

    def make_client_order_id(self, symbol, side):
        # Binance clientOrderId limit je kratak; ovaj format ostaje daleko ispod limita.
        suffix = uuid.uuid4().hex[:8]
        return f"T27_{side[0]}_{symbol}_{int(time.time())}_{suffix}"[:36]

    def _qty_tolerance(self, symbol, reference_qty=0.0):
        step = float(self.rules.get(symbol, {}).get("step_size", 0) or 0)
        return max(1e-12, step * 0.51, abs(float(reference_qty)) * 1e-8)

    def get_position_qty(self, symbol):
        pos = self.get_active_position(symbol)
        if not pos:
            return 0.0
        return abs(float(pos.get("positionAmt", 0) or 0))

    def finalize_pending_order(self, symbol, pending, order_info):
        """Primeni state tranziciju tek kada je Binance order potvrden kao FILLED."""
        action = pending.get('action')
        side = pending.get('side')
        client_id = pending.get('client_order_id')
        order_info = order_info if isinstance(order_info, dict) else {}
        order_id = order_info.get('orderId')
        executed_qty = self._number(order_info.get('executedQty'), 'order.executedQty')
        avg_fill_price = float(order_info.get('avgPrice', 0) or 0)
        cum_quote = float(order_info.get('cumQuote', 0) or 0)
        position_side = order_info.get('positionSide', 'LONG' if self.cfg.hedge_mode else 'BOTH')
        qty_before = abs(float(pending.get('position_qty_before', 0) or 0))
        level_before = pending.get('level_before')
        fill_ts = float(
            order_info.get('updateTime')
            or order_info.get('time')
            or (pending.get('created_at', time.time()) * 1000)
        ) / 1000.0

        if executed_qty <= 0:
            logger.error(
                f"CRITICAL {symbol}: order je FILLED ali executedQty={executed_qty}; pending ostaje zakljucan."
            )
            self.audit(
                "FILLED_WITHOUT_EXECUTED_QTY", symbol, severity="ERROR",
                client_order_id=client_id, order_id=order_id
            )
            return False

        if abs(executed_qty - float(pending['quantity'])) > self._qty_tolerance(symbol, executed_qty):
            self.error_rate_limited(f'filled_quantity:{symbol}:{client_id}',
                f'CRITICAL {symbol}: FILLED executedQty ne odgovara trazenoj kolicini; pending ostaje.')
            return False

        pos = None
        qty_after = None
        for _ in range(6):
            pos = self.get_active_position(symbol)
            qty_after = abs(float(pos.get('positionAmt', 0) or 0)) if pos else 0.0

            if action == 'CLOSE':
                if qty_after == 0.0:
                    break
            else:
                expected_after = qty_before + executed_qty
                if abs(qty_after - expected_after) <= self._qty_tolerance(symbol, expected_after):
                    break
            time.sleep(0.25)

        if action in ('ENTRY', 'DCA'):
            expected_after = qty_before + executed_qty
            tolerance = self._qty_tolerance(symbol, expected_after)
            if qty_after is None or abs(qty_after - expected_after) > tolerance:
                logger.error(
                    f"CRITICAL {symbol}: {action} je FILLED, ali position qty nije ocekivana "
                    f"(pre={qty_before}, executed={executed_qty}, expected={expected_after}, actual={qty_after}). "
                    f"Pending ostaje; potrebna je rucna provera."
                )
                self.audit(
                    "FILLED_POSITION_QTY_MISMATCH", symbol, severity="ERROR", action=action,
                    client_order_id=client_id, order_id=order_id, position_qty_before=qty_before,
                    executed_qty=executed_qty, expected_position_qty=expected_after,
                    position_qty_after=qty_after, level_before=level_before
                )
                return False

        if not self.reload_state():
            return False
        current = copy.deepcopy(self.locked_levels.get(symbol, self._default_state()))
        if current.get('pending_order') != pending:
            return False
        snapshot = pending.get('state_before') or {'level': level_before, 'base_price': None, 'open_time': None}
        manual_level = current.get('level') != snapshot.get('level')

        if action == 'ENTRY':
            if not pos or qty_after <= 0:
                logger.error(
                    f"CRITICAL {symbol}: ENTRY order je FILLED ali aktivna pozicija nije pronadena; pending ostaje zakljucan."
                )
                self.audit("ORDER_FILLED_POSITION_MISSING", symbol, severity="ERROR", client_order_id=client_id)
                return False

            base_price = float(pos.get('entryPrice', 0) or 0)
            if not math.isfinite(base_price) or base_price <= 0:
                logger.error(f"CRITICAL {symbol}: ENTRY FILLED ali entryPrice nije validan; pending ostaje.")
                return False

            if not self.update_state(
                symbol, level=current['level'] if manual_level else 0,
                base_price=base_price if current.get('base_price') == snapshot.get('base_price') else _UNCHANGED,
                open_time=fill_ts if current.get('open_time') == snapshot.get('open_time') else _UNCHANGED,
                pending_order=None, expected_state=current
            ):
                logger.error(f"CRITICAL {symbol}: order FILLED ali finalni ENTRY state nije snimljen.")
                return False
            level_after = self.locked_levels[symbol]['level']

        elif action == 'DCA':
            target_level = current['level'] if manual_level else pending['target_level']
            if not self.update_state(symbol, level=target_level, pending_order=None, expected_state=current):
                logger.error(f"CRITICAL {symbol}: DCA FILLED ali level={target_level} nije snimljen.")
                return False
            level_after = target_level

        elif action == 'CLOSE':
            tolerance = self._qty_tolerance(symbol, qty_before)
            if qty_after is None or qty_after != 0.0:
                logger.error(
                    f"CRITICAL {symbol}: CLOSE order je FILLED ali Binance jos pokazuje "
                    f"positionAmt={qty_after}; state se NE resetuje."
                )
                self.audit(
                    "CLOSE_FILLED_POSITION_REMAINS", symbol, severity="ERROR",
                    client_order_id=client_id, remaining_qty=qty_after
                )
                return False
            if not self.update_state(
                symbol, level=current['level'] if manual_level else 0,
                base_price=None if current.get('base_price') == snapshot.get('base_price') or 'state_before' not in pending else _UNCHANGED,
                open_time=None if current.get('open_time') == snapshot.get('open_time') or 'state_before' not in pending else _UNCHANGED,
                pending_order=None, expected_state=current
            ):
                logger.error(f"CRITICAL {symbol}: CLOSE FILLED ali state reset nije snimljen.")
                return False
            level_after = self.locked_levels[symbol]['level']
        else:
            logger.error(f"CRITICAL {symbol}: nepoznat pending action={action}; trading blokiran.")
            return False

        if side == 'BUY':
            self.last_trade_time[symbol] = time.time()
        elif side == 'SELL':
            self.last_close_time[symbol] = time.time()

        logger.info(
            f"ORDER_FILLED {symbol} action={action} side={side} qty={pending.get('quantity')} "
            f"executedQty={executed_qty} clientOrderId={client_id} orderId={order_id}"
        )
        self.audit(
            "ORDER_FILLED", symbol, action=action, side=side, position_side=position_side,
            quantity=pending.get('quantity'), executed_qty=executed_qty, avg_price=avg_fill_price,
            cum_quote=cum_quote, client_order_id=client_id, order_id=order_id, status='FILLED',
            position_qty_before=qty_before, position_qty_after=qty_after,
            level_before=level_before, level_after=level_after
        )

        # Telegram: prikazi samo operativne informacije koje su bitne za strategiju.
        # clientOrderId/orderId ostaju u logu i audit fajlu, ali se ne salju na Telegram.
        if avg_fill_price <= 0 and cum_quote > 0 and executed_qty > 0:
            avg_fill_price = cum_quote / executed_qty

        def _fmt(value, max_decimals=8):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return "N/A"
            if not math.isfinite(value):
                return "N/A"
            return f"{value:.{max_decimals}f}".rstrip('0').rstrip('.')

        if side == 'BUY' and action in ('ENTRY', 'DCA'):
            step_no = int(level_after) + 1
            reason_txt = 'ENTRY' if action == 'ENTRY' else 'DCA'

            # Nakon potvrdenog BUY fill-a Binance position entryPrice je novi AVG celog basketa.
            basket_avg = float(pos.get('entryPrice', 0) or 0) if pos else 0.0
            sell_target = None
            if basket_avg > 0 and 0 <= int(level_after) < len(self.cfg.profits):
                target_pct = float(self.cfg.profits[int(level_after)])
                sell_target = basket_avg * (1 + target_pct / 100.0)

            msg = (
                f"✅ <b>{symbol}</b>\n"
                f"Akcija: BUY\n"
                f"Razlog: {reason_txt}\n"
                f"Kolicina: {_fmt(executed_qty)}\n"
                f"Korak: {step_no}\n"
                f"Cena: {_fmt(avg_fill_price)}\n"
                f"Sell: {_fmt(sell_target)}"
            )

        elif side == 'SELL' and action == 'CLOSE':
            step_no = int(level_before) + 1
            ctx = pending.get('context') if isinstance(pending.get('context'), dict) else {}
            entry_before = float(ctx.get('entry_price', 0) or 0)
            target_price = float(ctx.get('target_price', 0) or 0)

            # Ovo je STRATEGIJSKI/GROSS ocekivani profit na TP matematici,
            # ne stvarni Binance PnL. Ne ukljucuje fee, funding ni slippage.
            expected_profit = None
            if entry_before > 0 and target_price > 0 and qty_before > 0:
                expected_profit = (target_price - entry_before) * qty_before

            msg = (
                f"💰 <b>{symbol}</b>\n"
                f"Akcija: SELL\n"
                f"Kolicina: {_fmt(executed_qty)}\n"
                f"Korak: {step_no}\n"
                f"Cena: {_fmt(avg_fill_price)}\n"
                f"Profit: {_fmt(expected_profit, 2)} USDT"
            )

        else:
            # Defensive fallback; trenutno ne bi trebalo da se koristi.
            msg = (
                f"<b>{symbol}</b>\n"
                f"Akcija: {side}\n"
                f"Kolicina: {_fmt(executed_qty)}\n"
                f"Cena: {_fmt(avg_fill_price)}"
            )

        self.tg.send(msg)
        return True

    def reconcile_pending_order(self, symbol, initial_order=None, aggressive=False):
        """Berza potvrdjuje izvrsenje naloga; STATE odredjuje nivo strategije."""
        if not self.state_reload_healthy:
            return False
        pending = self.locked_levels.get(symbol, {}).get('pending_order')
        if pending is None:
            return True
        try:
            self._validate_pending(pending)
            client_id = pending['client_order_id']
            order = initial_order
            if not isinstance(order, dict) or not order.get('status'):
                order = self._api_call('futures_get_order', symbol=symbol, origClientOrderId=client_id)
            if not isinstance(order, dict):
                raise ValueError('Order odgovor nije objekat')
            for field, expected in (('symbol', symbol), ('clientOrderId', client_id), ('side', pending['side']), ('positionSide', 'LONG')):
                if order.get(field) != expected:
                    raise ValueError(f'Order odgovor ima pogresan {field}')
            status = order.get('status')
            self.audit('ORDER_RECONCILE', symbol, client_order_id=client_id, order_id=order.get('orderId'),
                       status=status, executed_qty=order.get('executedQty'), avg_price=order.get('avgPrice'),
                       cum_quote=order.get('cumQuote'), level_before=pending.get('level_before'),
                       position_qty_before=pending.get('position_qty_before'))
            if status == 'FILLED':
                return self.finalize_pending_order(symbol, pending, order)
            if status in ('CANCELED', 'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'):
                executed = self._number(order.get('executedQty'), 'order.executedQty', allow_zero=True)
                if executed == 0:
                    self.audit('ORDER_TERMINAL_FAILURE', symbol, client_order_id=client_id, status=status, executed_qty=0)
                    self._clear_pending(symbol, client_id)
                else:
                    self.error_rate_limited(f'partial:{symbol}:{client_id}',
                        f'CRITICAL {symbol}: {status} sa executedQty={executed}; pending ostaje, potrebna rucna provera.')
                return False
            if status == 'NEW':
                # Normalno kratko prelazno stanje: nalog je prihvacen, ceka se fill.
                # Ostaje INFO u log fajlu, ali je zuto u konzoli radi vidljivosti.
                logger.info(
                    f'{symbol}: order accepted | status=NEW | cekam fill; novi orderi za simbol su privremeno blokirani.',
                    extra={"yellow": True}
                )
            else:
                # Drugi neterminalni statusi su redji i ostaju pravi WARNING.
                self.warn_rate_limited(
                    f'pending:{symbol}:{client_id}',
                    f'{symbol}: pending {client_id} status={status}; novi orderi su blokirani.',
                    60
                )
            return False
        except ApiBackoffError:
            return False
        except Exception as e:
            # -2013 nikada ne dokazuje da BUY nije izvrsen. Nema isteka pending-a.
            client_id = pending.get('client_order_id') if isinstance(pending, dict) else '?'
            self.error_rate_limited(f'pending_unknown:{symbol}:{client_id}',
                f'CRITICAL {symbol}: pending {client_id} nije razresen ({e}); ostaje u STATE-u, nema novih ordera.')
            self.audit('ORDER_STATUS_UNKNOWN', symbol, severity='ERROR', client_order_id=client_id,
                       error=str(e), binance_code=getattr(e, 'code', None))
            return False

    def execute_trade(self, symbol, side, quantity, reason='', action=None, target_level=None,
                      context=None, position_qty_before=None, level_before=None):
        """Fresh BUY check -> persistent pending -> jedan send -> reconcile."""
        action = action or ('ENTRY' if side == 'BUY' else 'CLOSE')
        try:
            if not self.reload_state() or self.locked_levels.get(symbol, {}).get('pending_order') is not None:
                return False
            if self.cfg.hedge_mode is not True or self.cfg.leverage != 1 or self.cfg.margin_type != 'CROSSED':
                return False
            prepared = None
            if side == 'BUY':
                prepared = self._prepare_buy(symbol, action, target_level, level_before, position_qty_before)
                if prepared is None:
                    return False
                price, quantity = prepared['price'], prepared['quantity']
                position_qty_before = prepared['position_qty_before']
                state_before = prepared['state']
                context = dict(context or {}, checked_price=price, checked_limit=prepared['limit'])
            elif side == 'SELL' and action == 'CLOSE':
                state_before = copy.deepcopy(self.locked_levels.get(symbol, self._default_state()))
                if state_before.get('level') != level_before:
                    return False
                price = self.get_market_price(symbol)
                if position_qty_before is None:
                    position_qty_before = self.get_position_qty(symbol)
            else:
                return False
            quantity = self._number(quantity, 'order.quantity')
            if quantity * price < 5:
                return False
            client_id = self.make_client_order_id(symbol, side)
            pending = dict(client_order_id=client_id, action=action, side=side, quantity=quantity,
                           reason=reason, created_at=time.time(), target_level=target_level,
                           level_before=level_before, position_qty_before=position_qty_before,
                           price_before=price, context=context or {},
                           state_before={k: state_before.get(k) for k in ('level', 'base_price', 'open_time')})
            if not self.update_state(symbol, pending_order=pending, expected_state=state_before):
                return False
            self.audit('ORDER_SEND', symbol, action=action, side=side, quantity=quantity, approx_price=price,
                       client_order_id=client_id, target_level=target_level, level_before=level_before,
                       position_qty_before=position_qty_before, context=context or {})
            logger.info(f'ORDER_SEND {symbol} action={action} side={side} qty={quantity} clientOrderId={client_id}')
            # Uhvati i rucnu izmenu nastalu tokom pending-save/log upisa, pre API slanja.
            expected_pending_state = copy.deepcopy(state_before)
            expected_pending_state['pending_order'] = pending
            if not self.reload_state():
                return False
            if self.locked_levels.get(symbol) != expected_pending_state:
                self.audit('ORDER_ABORTED_BEFORE_SEND', symbol, client_order_id=client_id, reason='state_changed')
                self._clear_pending(symbol, client_id)
                return False
            # Disk/log zastoj ne sme pretvoriti staru cenu u novu dozvolu za BUY.
            if prepared and (time.monotonic() - prepared['started'] > 5.0 or
                             (prepared['candle_minute'] is not None and int(time.time() // 60) != prepared['candle_minute'])):
                self.audit('BUY_ABORTED_BEFORE_SEND', symbol, client_order_id=client_id, reason='expired_preflight')
                self._clear_pending(symbol, client_id)  # Poznato neposlat nalog, bez Binance zahteva.
                return False
            try:
                response = self._api_call('futures_create_order', symbol=symbol, side=side, type='MARKET',
                                          quantity=quantity, newClientOrderId=client_id, positionSide='LONG', recvWindow=5000)
            except ApiBackoffError:
                self.audit('ORDER_ABORTED_BEFORE_SEND', symbol, client_order_id=client_id, reason='local_api_backoff')
                self._clear_pending(symbol, client_id)  # Lokalna pauza, create_order nije pozvan.
                return False
            except Exception as e:
                self.audit('ORDER_SEND_EXCEPTION', symbol, severity='ERROR', client_order_id=client_id,
                           error=str(e), binance_code=getattr(e, 'code', None))
                if isinstance(e, BinanceAPIException) and getattr(e, 'code', None) in (-2019, -1008):
                    # Uska lista dokumentovanih odbijanja; timeout/5xx/-2013 nisu na listi.
                    self.audit('ORDER_DEFINITELY_REJECTED', symbol, client_order_id=client_id, binance_code=e.code)
                    if side == 'BUY':
                        self._buy_retry_until[symbol] = time.monotonic() + 30.0
                    self._clear_pending(symbol, client_id)
                    return False
                logger.error(f'CRITICAL {symbol}: odgovor na slanje {client_id} nije pouzdan; proveravam sacuvan nalog.')
                return self.reconcile_pending_order(symbol)
            self.audit('ORDER_RESPONSE', symbol, client_order_id=client_id, response=response)
            return self.reconcile_pending_order(symbol, initial_order=response)
        except ApiBackoffError:
            return False
        except Exception as e:
            logger.error(f'CRITICAL Trade Error {symbol}: {e}')
            self.audit('TRADE_ERROR', symbol, severity='ERROR', error=str(e), side=side, reason=reason)
            return False

    def check_position_age_alert(self, symbol, pos_data, price, current_level):
        """Posle 120d samo alarmira. Ne zatvara poziciju i ne blokira TP/DCA."""
        open_time = self.ensure_open_time(symbol, pos_data)
        if not open_time:
            return

        now = time.time()
        age_days = (now - open_time) / 86400.0
        if age_days < self.cfg.position_age_alert_days:
            self._age_alert_cache.pop(symbol, None)
            return

        last_alert = self._age_alert_cache.get(symbol, 0)
        if now - last_alert < self.cfg.position_age_alert_every_sec:
            return
        self._age_alert_cache[symbol] = now

        amt = abs(float((pos_data or {}).get('positionAmt', 0) or 0))
        entry = float((pos_data or {}).get('entryPrice', 0) or 0)
        pnl_usd = (price - entry) * amt if price is not None and entry > 0 else None
        pnl_pct = ((price - entry) / entry * 100.0) if price is not None and entry > 0 else None
        price_txt = f'{price:.6f}' if price is not None else 'API UNKNOWN'
        pnl_txt = f'{pnl_usd:+.2f} USDT ({pnl_pct:+.1f}%)' if pnl_usd is not None else 'API UNKNOWN'
        opened_txt = datetime.fromtimestamp(open_time).strftime('%Y-%m-%d %H:%M:%S')

        text = (
            f"🔴🔴🔴 POSITION AGE ALERT {symbol} | {age_days:.1f}d | "
            f"LEVEL={current_level} | Avg={entry:.6f} | Price={price_txt} | "
            f"PnL={pnl_txt} | Opened={opened_txt} | "
            f"MANUAL REVIEW/CLOSE REQUIRED | STOP BOT BEFORE MANUAL CLOSE"
        )
        logger.error(text)
        self.audit(
            "POSITION_AGE_ALERT", symbol, severity="ERROR", age_days=age_days,
            alert_after_days=self.cfg.position_age_alert_days, level=current_level,
            entry_price=entry, current_price=price, pnl_usd=pnl_usd, pnl_pct=pnl_pct,
            open_time=open_time
        )

        tg_msg = (
            f"🔴🔴🔴 <b>{symbol} POSITION AGE ALERT</b>\n"
            f"Starost: <b>{age_days:.1f} dana</b>\nLevel: {current_level}\n"
            f"Avg: {entry:.6f}\nCena: {price_txt}\nPnL: {pnl_txt}\n"
            f"Otvoreno: {opened_txt}\n<b>MANUAL REVIEW / CLOSE REQUIRED</b>\n"
            f"<b>STOP BOT BEFORE MANUAL CLOSE</b>"
        )
        self.tg.send(tg_msg, critical=True)

    def get_fear_and_greed(self):
        if time.time() - self.fng_last_update < 3600:
            return self.fng_value
        try:
            url = "https://api.alternative.me/fng/"
            r = requests.get(url, timeout=10).json()
            self.fng_value = int(r['data'][0]['value'])
            self.fng_last_update = time.time()
            return self.fng_value
        except Exception as e:
            self.warn_rate_limited("fng_api", f"FNG API problem, koristim poslednju vrednost {self.fng_value}: {e}", 300)
            return self.fng_value

    def check_entry_signal(self, symbol, current_price):
        try:
            limit = self._entry_limit(symbol)
            return limit is not None and current_price <= limit
        except ApiBackoffError:
            return False
        except Exception as e:
            self.warn_rate_limited(f'entry_signal:{symbol}', f'{symbol}: ENTRY provera nije uspela: {e}', 60)
            return False

    def smart_round(self, symbol, quantity):
        rule = self.rules.get(symbol)
        if not rule: return quantity
        step = rule['step_size']
        precision = rule['qty_prec']
        qty = int(quantity / step) * step
        return float(f"{qty:.{precision}f}")

    def get_dca_level(self, current_size):
        base = self.cfg.base_order_size
        risks = self.cfg.risks
        if current_size < base * 1.25: return 0 
        expected_size = base
        for i, r in enumerate(risks):
            expected_size += (base * r)
            if current_size <= expected_size * 1.15: return i + 1
        return len(risks) + 1 


    def report_status(self):
        print(f"\n📊 --- STATUS ({datetime.now().strftime('%H:%M')}) ---")
        for symbol in self.cfg.symbols:
            try:
                pos = self.get_active_position(symbol)
                price = self.get_market_price(symbol)

                stored = self.locked_levels.get(symbol, {})
                try:
                    lock_level = int(stored.get("level", 0))
                except Exception:
                    lock_level = 0

                if not pos or float(pos.get('positionAmt', 0)) == 0:
                    entry_limit = self._entry_limit(symbol)

                    def _status_price(value):
                        if value is None:
                            return "N/A"
                        value = float(value)
                        return f"{value:.8f}".rstrip('0').rstrip('.')

                    if entry_limit is None:
                        entry_txt = "BLOKIRAN"
                    else:
                        entry_txt = f"<= {_status_price(entry_limit)} ({self.cfg.lookback}m low)"

                    print(
                        f"🔹 {symbol}: Nema pozicije | Cena {_status_price(price)} | Entry {entry_txt}"
                    )
                    continue

                amt = float(pos['positionAmt'])
                entry = float(pos['entryPrice'])  # Binance avg entry
                size = abs(amt) * entry

                # PosLevel (heuristika) je samo informativan
                pos_level = self.get_dca_level(size)

                # Baza (anchor) mora doci iz state-a; matematicka procena je samo INFO.
                valid_level, base_price, state_error = self.get_valid_position_state(symbol)
                state_txt = ""
                if state_error:
                    state_txt = f" | STATE INVALID: {state_error}"
                    base_price = self.get_real_base_price(symbol, entry, amt, allow_estimate=True)

                target_txt = "STATE INVALID"
                if state_error is None:
                    p_idx = min(valid_level, len(self.cfg.profits) - 1)
                    target = entry * (1 + (self.cfg.profits[p_idx] / 100))
                    target_txt = f"{target:.4f}"

                buy_txt = "STATE INVALID"
                if state_error is None and valid_level < len(self.cfg.drops):
                    drop_pct = self.cfg.drops[valid_level]
                    next_price = base_price * (1 - (drop_pct / 100))
                    buy_txt = f"{next_price:.4f}$ (-{drop_pct}%) [Base {base_price:.4f}]"
                elif state_error is None:
                    buy_txt = "MAX"

                pos_txt = f"Pos {pos_level}"
                if abs(lock_level - pos_level) >= 2:
                    pos_txt = f"⚠️Pos {pos_level}"

                open_time = stored.get("open_time")
                age_txt = "?"
                if open_time:
                    try:
                        age_txt = f"{(time.time() - float(open_time)) / 86400.0:.1f}d"
                    except Exception:
                        age_txt = "?"
                pending_txt = " | PENDING" if stored.get("pending_order") else ""

                print(
                    f"🔸 {symbol} [LEVEL {lock_level} | {pos_txt}]: "
                    f"Size {size:.1f}$ | Avg {entry:.4f} | TP {target_txt} | "
                    f"Age {age_txt} | Next DCA {buy_txt}{pending_txt}{state_txt}"
                )
            except Exception as e:
                logger.warning(f"STATUS {symbol} nije mogao da se izracuna: {e}")
        print("----------------------------------------------\n")
    def process_symbol(self, symbol):
        try:
            self.reload_state()
            pos_data, price = None, None
            position_ok = False
            try:
                pos_data = self.get_active_position(symbol)
                position_ok = True
            except ApiBackoffError:
                pass
            except Exception as e:
                self.error_rate_limited(f'position_read:{symbol}', f'{symbol}: pozicija nije potvrdjena: {e}')
            had_pending = self.locked_levels.get(symbol, {}).get('pending_order') is not None
            if self.state_reload_healthy and had_pending:
                self.reconcile_pending_order(symbol)
            try:
                price = self.get_market_price(symbol)
            except ApiBackoffError:
                pass
            except Exception as e:
                self.error_rate_limited(f'price_read:{symbol}', f'{symbol}: cena nije potvrdjena: {e}')
            pos_amt = float(pos_data['positionAmt']) if pos_data else 0.0
            entry_price = float(pos_data['entryPrice']) if pos_data else 0.0
            stored = self.locked_levels.get(symbol, {})
            if pos_amt != 0 or self._cycle_recorded(stored):
                self.check_position_age_alert(symbol, pos_data, price, stored.get('level'))
            if had_pending or not position_ok or price is None or not self.state_reload_healthy or symbol in self.disabled_symbols:
                return

            # --- 1. NEMA POZICIJE (ULAZ) ---
            if pos_amt == 0:
                current_stored = self.locked_levels.get(symbol, {})
                if (
                    current_stored.get("level", 0) != 0
                    or current_stored.get("base_price") is not None
                    or current_stored.get("open_time") is not None
                ):
                    self.error_rate_limited(f'unexpected_flat:{symbol}',
                        f'CRITICAL {symbol}: Binance prikazuje nulu, STATE ima otvoren ciklus. '
                        f'Nema automatskog resetovanja/P1. Proveri poziciju i naloge; posle rucnog FULL close-a potvrdi reset u STATE-u.')
                    return

                last_close = self.last_close_time.get(symbol, 0)
                if (time.time() - last_close) / 60 < self.cfg.entry_cooldown_min:
                    return

                if self.check_entry_signal(symbol, price):
                    qty = self.smart_round(symbol, self.cfg.base_order_size / price)
                    self.audit(
                        "ENTRY_TRIGGER", symbol, price=price, quantity=qty,
                        base_order_usd=self.cfg.base_order_size
                    )
                    self.execute_trade(
                        symbol, 'BUY', qty, "START - Entry", action='ENTRY',
                        context={"trigger_price": price, "level": 0},
                        position_qty_before=0.0, level_before=0
                    )
                return

            # --- 2. IMA POZICIJE (MENADZMENT) ---
            current_notional = abs(pos_amt) * entry_price
            if current_notional < 5:
                return

            current_level, real_base_price, state_error = self.get_valid_position_state(symbol)
            if state_error is not None:
                self.error_rate_limited(
                    f"position_state_invalid:{symbol}:{state_error}",
                    f"CRITICAL {symbol}: aktivna LONG pozicija postoji, ali state nije pouzdan "
                    f"({state_error}). TP i DCA SU BLOKIRANI do rucne popravke bot_state.json.",
                    every_sec=300
                )
                self.audit(
                    "POSITION_STATE_INVALID", symbol, severity="ERROR", reason=state_error,
                    position_amt=pos_amt, entry_price=entry_price, position_notional=current_notional
                )
                return

            pos_level = self.get_dca_level(current_notional)

            # Informativni mismatch warning. Validan state ostaje autoritativan.
            if abs(current_level - pos_level) >= 2:
                now_ts = time.time()
                prev = self._mismatch_warn_cache.get(symbol)
                warn_every = float(self.cfg.data.get("extra", {}).get("mismatchWarnEverySec", 0) or 0)
                should_log = (
                    prev is None
                    or prev[0] != pos_level
                    or prev[1] != current_level
                    or (warn_every > 0 and (now_ts - prev[2]) >= warn_every)
                )
                if should_log:
                    logger.warning(
                        f"⚠️ {symbol}: PosLevel={pos_level} se razlikuje od LockLevel={current_level}. "
                        f"Validan state je autoritativan; DCA nastavlja po state-u."
                    )
                    self.audit(
                        "LEVEL_MISMATCH", symbol, severity="WARNING",
                        pos_level=pos_level, state_level=current_level, notional=current_notional
                    )
                    self._mismatch_warn_cache[symbol] = (pos_level, current_level, now_ts)

            # A. PROFIT
            # LEVEL 0=P1->profit[0], LEVEL 1=P2->profit[1], itd.
            prof_idx = min(current_level, len(self.cfg.profits) - 1)
            target_pct = self.cfg.profits[prof_idx]
            take_profit_price = entry_price * (1 + (target_pct / 100))

            if price >= take_profit_price:
                self.audit(
                    "TP_TRIGGER", symbol, level=current_level, price=price,
                    entry_price=entry_price, target_price=take_profit_price, target_pct=target_pct,
                    position_amt=pos_amt
                )
                self.execute_trade(
                    symbol, 'SELL', abs(pos_amt), f"Take Profit (Level {current_level})",
                    action='CLOSE',
                    context={
                        "level": current_level, "entry_price": entry_price,
                        "target_price": take_profit_price, "trigger_price": price
                    },
                    position_qty_before=abs(pos_amt), level_before=current_level
                )
                return

            # B. DOKUP (DCA)
            if current_level < len(self.cfg.drops):
                drop_pct = self.cfg.drops[current_level]
                dca_price = real_base_price * (1 - (drop_pct / 100))

                if price <= dca_price:
                    target_level = current_level + 1
                    stored_lvl = self.locked_levels.get(symbol, {}).get("level", 0)
                    if stored_lvl >= target_level:
                        return

                    req_wait = 0
                    if current_level < len(self.cfg.dca_cooldowns_min):
                        req_wait = self.cfg.dca_cooldowns_min[current_level]

                    last_trade = self.last_trade_time.get(symbol, 0)
                    if (time.time() - last_trade) / 60 < req_wait:
                        return

                    risk_factor = self.cfg.risks[current_level]
                    buy_value = self.cfg.base_order_size * risk_factor
                    qty = self.smart_round(symbol, buy_value / price)

                    self.audit(
                        "DCA_TRIGGER", symbol, from_level=current_level, to_level=target_level,
                        trigger_price=price, dca_price=dca_price, base_price=real_base_price,
                        entry_price=entry_price, position_amt=pos_amt, position_notional=current_notional,
                        risk_factor=risk_factor, buy_value_usd=buy_value, quantity=qty
                    )
                    self.execute_trade(
                        symbol, 'BUY', qty,
                        f"DCA Dokup (Level {current_level} -> {target_level}) [Base: {real_base_price:.2f}]",
                        action='DCA', target_level=target_level,
                        context={
                            "from_level": current_level, "to_level": target_level,
                            "dca_price": dca_price, "trigger_price": price,
                            "base_price": real_base_price, "entry_price_before": entry_price
                        },
                        position_qty_before=abs(pos_amt), level_before=current_level
                    )

        except Exception as e:
            logger.error(f"CRITICAL Greska {symbol}: {e}")
            self.audit("PROCESS_SYMBOL_ERROR", symbol, severity="ERROR", error=str(e))

    def run(self):
        if not self.setup():
            logger.error("CRITICAL: setup nije uspeo. Bot2027 v2 se ne pokrece.")
            return

        logger.info("🤖 Bot2027 v2 Multi-Coin Bot je aktivan.")
        last_h, last_r = 0, 0
        while True:
            for s in self.cfg.symbols:
                self.process_symbol(s)
                time.sleep(self.cfg.symbol_sleep_sec)

            if time.time() - last_h > 300:
                print("💓 Heartbeat...")
                last_h = time.time()

            if time.time() - last_r > 3600:
                self.report_status()
                last_r = time.time()

            time.sleep(0.5)

    def _number(self, value, name, minimum=0.0, allow_zero=False):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f'{name}: nevalidan broj')
        n = float(value)
        if not math.isfinite(n) or n < minimum or (not allow_zero and n == minimum):
            raise ValueError(f'{name}: nevalidan broj {value!r}')
        return n

    def _validate_level(self, value):
        if type(value) is not int or not 0 <= value <= len(self.cfg.drops):
            raise ValueError('level mora biti ceo broj u dozvoljenom DCA opsegu')
        return value

    def _validate_pending(self, pending):
        if pending is None:
            return
        if not isinstance(pending, dict) or not pending:
            raise ValueError('pending_order mora biti potpun objekat ili null')
        cid = pending.get('client_order_id')
        if not isinstance(cid, str) or not re.fullmatch(r'[.A-Za-z0-9_:/-]{1,36}', cid):
            raise ValueError('pending_order: nevalidan client_order_id')
        action = pending.get('action')
        if action not in ('ENTRY', 'DCA', 'CLOSE') or pending.get('side') != ('SELL' if action == 'CLOSE' else 'BUY'):
            raise ValueError('pending_order: nevalidan action/side')
        for field in ('quantity', 'created_at', 'price_before'):
            self._number(pending.get(field), f'pending.{field}')
        before = self._number(pending.get('position_qty_before'), 'pending.position_qty_before', allow_zero=True)
        level = self._validate_level(pending.get('level_before'))
        if action == 'DCA':
            target = self._validate_level(pending.get('target_level'))
            if target != level + 1 or before <= 0:
                raise ValueError('pending DCA mora imati target_level=level_before+1 i prethodnu poziciju')
        elif action == 'ENTRY' and before != 0:
            raise ValueError('pending ENTRY mora poceti sa nultom pozicijom')
        elif action == 'CLOSE' and before <= 0:
            raise ValueError('pending CLOSE mora imati prethodnu poziciju')
        # Ne porediti level_before sa trenutnim STATE nivoom: korisnik sme da ga promeni.

    def _cycle_recorded(self, state):
        return state.get('level', 0) != 0 or state.get('base_price') is not None or state.get('open_time') is not None

    def _api_call(self, method, **kwargs):
        """Jedan poziv, bez automatskog ponavljanja naloga; backoff je po endpointu/simbolu."""
        now = time.monotonic()
        key = (method, kwargs.get('symbol', ''))
        failures, retry_at = self._api_retry.get(key, (0, 0.0))
        if now < max(self._api_global_until, retry_at):
            raise ApiBackoffError(f'API pauza: {method} {key[1]}')
        try:
            result = getattr(self.client, method)(**kwargs)
        except Exception as e:
            status = getattr(e, 'status_code', None)
            code = getattr(e, 'code', None)
            delay = min(60.0, 5.0 * 2 ** min(failures, 4))
            if status in (418, 429) or code == -1003:
                delay = max(delay, 120.0 if status == 418 else 60.0)
                response = getattr(e, 'response', None)
                header = (getattr(response, 'headers', None) or {}).get('Retry-After')
                if header:
                    try:
                        wait = float(header)
                    except (ValueError, TypeError):
                        try:
                            wait = parsedate_to_datetime(header).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            wait = 0.0
                    if math.isfinite(wait):
                        delay = max(delay, wait)
                ban = re.search(r'banned until (\d{10,13})', str(e), re.IGNORECASE)
                if ban:
                    until = float(ban.group(1))
                    if until > 1e11:
                        until /= 1000.0
                    delay = max(delay, until - time.time())
                self._api_global_until = time.monotonic() + delay
            # BUY rejection must not delay a later protective LONG SELL on this endpoint.
            if method != 'futures_create_order':
                self._api_retry[key] = (failures + 1, time.monotonic() + delay)
            self.warn_rate_limited(f'api:{key}', f'API problem {method} {key[1]}: {e}', 60)
            raise
        if failures:
            logger.info(f'API RECOVERED: {method} {key[1]}; pre BUY-a sledi provera stanja i nov signal.')
        self._api_retry.pop(key, None)
        return result

    def _clear_pending(self, symbol, client_id):
        if not self.reload_state():
            return False
        current = copy.deepcopy(self.locked_levels.get(symbol, self._default_state()))
        pending = current.get('pending_order')
        if pending is None or pending.get('client_order_id') != client_id:
            return False
        return self.update_state(symbol, pending_order=None, expected_state=current)

    def _entry_limit(self, symbol):
        if self.cfg.fng_enabled and self.get_fear_and_greed() > self.cfg.fng_max:
            return None

        klines = self._api_call(
            'futures_klines',
            symbol=symbol,
            interval='1m',
            limit=self.cfg.lookback + 1
        )

        if not isinstance(klines, list) or len(klines) < self.cfg.lookback:
            raise ValueError('Nedovoljan broj 1m sveca za ENTRY')

        current_minute = int(time.time() // 60) * 60000
        last_open = int(klines[-1][0])

        # Binance je vec vratio novu, trenutno otvorenu svecu.
        if last_open == current_minute:
            history = klines[-(self.cfg.lookback + 1):-1]

        # Na samom prelazu minuta nova sveca jos nije stigla u REST odgovor.
        # Poslednja vracena sveca je upravo zavrsena i legitimna je za lookback.
        elif last_open == current_minute - 60000:
            history = klines[-self.cfg.lookback:]

        else:
            raise ValueError(
                f'ENTRY 1m podaci su zastareli: '
                f'last_open={last_open}, expected={current_minute} ili {current_minute - 60000}'
            )

        if len(history) != self.cfg.lookback:
            raise ValueError('Nedovoljan broj zavrsenih 1m sveca za ENTRY')

        expected_first = current_minute - self.cfg.lookback * 60000

        if any(
            int(k[0]) != expected_first + i * 60000
            for i, k in enumerate(history)
        ):
            raise ValueError('ENTRY zavrsene 1m svece nisu neprekinut lookback')

        return min(
            self._number(k[3], 'kline.low')
            for k in history
        )

    def _prepare_buy(self, symbol, action, target_level, level_before, position_qty_before):
        """STATE + nova pozicija + nov signal + nova cena. Nema queued BUY odluke."""
        started = time.monotonic()
        if started < self._buy_retry_until.get(symbol, 0):
            return None
        if not self.reload_state() or symbol in self.disabled_symbols:
            return None
        state = copy.deepcopy(self.locked_levels.get(symbol, self._default_state()))
        if state.get('pending_order') is not None:
            return None
        if level_before is None or state.get('level', 0) != level_before:
            return None  # Rucni edit je zakon; stara odluka se odbacuje, sledeci loop koristi novi nivo.
        pos = self.get_active_position(symbol)
        qty_before = float(pos['positionAmt']) if pos else 0.0
        if position_qty_before is None or abs(qty_before - position_qty_before) > self._qty_tolerance(symbol, qty_before):
            self.error_rate_limited(f'buy_position_changed:{symbol}', f'{symbol}: pozicija promenjena tokom BUY odluke; BUY preskocen.')
            return None
        if action == 'ENTRY':
            if qty_before != 0 or self._cycle_recorded(state):
                return None
            if (time.time() - self.last_close_time.get(symbol, 0)) / 60 < self.cfg.entry_cooldown_min:
                return None
            limit = self._entry_limit(symbol)
            budget = self.cfg.base_order_size
            candle_minute = int(time.time() // 60)
        elif action == 'DCA':
            level, base, error = self.get_valid_position_state(symbol)
            if error or qty_before <= 0 or level >= len(self.cfg.drops) or target_level != level + 1:
                return None
            wait = self.cfg.dca_cooldowns_min[level] if level < len(self.cfg.dca_cooldowns_min) else 0
            if (time.time() - self.last_trade_time.get(symbol, 0)) / 60 < wait:
                return None
            limit = base * (1 - self.cfg.drops[level] / 100)
            budget = self.cfg.base_order_size * self.cfg.risks[level]
            candle_minute = None
        else:
            return None
        price = self.get_market_price(symbol)
        if limit is None or price > limit or time.monotonic() - started > 5.0:
            return None
        if candle_minute is not None and int(time.time() // 60) != candle_minute:
            return None
        quantity = self.smart_round(symbol, budget / price)
        return dict(price=price, quantity=quantity, position_qty_before=qty_before,
                    state=state, started=started, candle_minute=candle_minute, limit=limit)

def main():
    instance_lock = SingleInstanceLock(INSTANCE_LOCK_FILE)
    if not instance_lock.acquire():
        logger.error(
            f"CRITICAL: Bot2027 v2 vec radi ili lock nije dostupan: {INSTANCE_LOCK_FILE}. "
            f"Druga instanca se gasi bez Binance poziva."
        )
        return 2

    try:
        bot = MultiCoinBot()
        bot.run()
        return 0
    except StateLoadError as e:
        logger.error(f"CRITICAL: {e}. Bot2027 v2 se ne pokrece.")
        return 3
    except KeyboardInterrupt:
        logger.info("Bot2027 v2 zaustavljen od strane korisnika.")
        return 0
    except Exception as e:
        logger.exception(f"CRITICAL UNHANDLED BOT2027 V2 ERROR: {e}")
        return 1
    finally:
        instance_lock.release()


if __name__ == "__main__":
    sys.exit(main())
