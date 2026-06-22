import json
import time
import logging
import requests 
import os
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ======================================================================================
# PODEŠAVANJE LOGOVANJA
# ======================================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("bot_log.txt", mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("MultiBot")

# Sentinel za update_state (omogucava eksplicitno brisanje base_price)
_UNCHANGED = object()

# ======================================================================================
# TELEGRAM MODUL
# ======================================================================================
class TelegramBot:
    def __init__(self, token, chat_id, enabled=True):
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, message):
        if not self.enabled: return
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            requests.post(self.base_url, data=payload, timeout=5)
        except Exception as e:
            logger.error(f"Ne mogu da pošaljem Telegram poruku: {e}")

# ======================================================================================
# KLASA ZA KONFIGURACIJU
# ======================================================================================
class Config:
    def __init__(self, filename="config.json"):
        with open(filename, "r") as f:
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
        self.tg_token = self.data['extra'].get('telegramToken') or os.getenv('TELEGRAM_BOT_TOKEN', '')
        
        self.fng_enabled = self.data['extra'].get('fng_enabled', False)
        self.fng_max = self.data['extra'].get('fng_buy_max', 30)
        
        self.base_order_size = float(self.data['capital']['min']) 
        self.drops = self.data['capital']['drop'] 
        self.risks = self.data['capital']['risk'] 
        self.profits = self.data['capital']['profit'] 
        
        self.lookback = int(self.data['price']['lookback'])
        
        self.entry_cooldown_min = self.data['order'].get('entryCooldown', 0)
        self.dca_cooldowns_min = self.data['order']['open'].get('cooldown', [])

# ======================================================================================
# GLAVNA KLASA BOTA
# ======================================================================================
class MultiCoinBot:
    def __init__(self):
        self.cfg = Config()
        self.client = Client(self.cfg.api_key, self.cfg.api_secret)
        self.tg = TelegramBot(self.cfg.tg_token, self.cfg.tg_chat_id, self.cfg.tg_enabled)
        self.rules = {} 
        
        self.fng_value = 50
        self.fng_last_update = 0
        
        self.last_trade_time = {} 
        self.last_close_time = {} 

        # --- STATE LOCK SYSTEM (NOVI DIZAJN) ---
        self.state_file = os.path.join(os.path.dirname(__file__), "bot_state.json")
        # Ucitavamo state. Ako je stari format, on ce se konvertovati u novi.
        self.locked_levels = self.load_state()
        self._mismatch_warn_cache = {}
        logger.info(f"STATE_FILE: {self.state_file}")
        try:
            for _s,_v in self.locked_levels.items():
                if isinstance(_v, dict):
                    logger.info(f"STATE_LOAD {_s}: level={_v.get('level')} base={_v.get('base_price')} open_time={_v.get('open_time')}")
        except Exception:
            pass
 
    # --- NOVE FUNKCIJE ZA CUVANJE STANJA ---
    def load_state(self):
        """Ucitava state. Vrsi migraciju sa starog formata (int) na novi (dict)."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    raw_data = json.load(f)
                    
                new_data = {}
                for sym, val in raw_data.items():
                    # Ako je stari format (samo broj)
                    if isinstance(val, int):
                        new_data[sym] = {"level": val, "base_price": None, "open_time": None}
                    # Ako je novi format (dict)
                    elif isinstance(val, dict):
                        new_data[sym] = {
                            "level": int(val.get("level", 0)),
                            "base_price": val.get("base_price", None),
                            "open_time": val.get("open_time", val.get("openTime", None))
                        }
                    else:
                        new_data[sym] = {"level": 0, "base_price": None, "open_time": None}
                return new_data
            except:
                return {}
        return {}

    def save_state(self):
        """Snima trenutno stanje u fajl."""
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.locked_levels, f, indent=4)
        except Exception as e:
            logger.error(f"Greska pri snimanju stanja: {e}")

    
    def reload_state(self):
        """Reload state from disk so manual edits take effect without restart."""
        try:
            self.locked_levels = self.load_state()
        except Exception:
            pass

    def update_state(self, symbol, level=None, base_price=_UNCHANGED, open_time=_UNCHANGED):
        """Pomocna funkcija za azuriranje i snimanje.

        base_price koristi sentinel _UNCHANGED da bi se razlikovalo:
        - nije prosledjeno (ne diraj postojecu vrednost)
        - prosledjeno None (obrisi base_price)
        - prosledjena float vrednost (setuj base_price)
        """
        if symbol not in self.locked_levels or not isinstance(self.locked_levels.get(symbol), dict):
            self.locked_levels[symbol] = {"level": 0, "base_price": None, "open_time": None}

        if level is not None:
            self.locked_levels[symbol]["level"] = int(level)

        if base_price is not _UNCHANGED:
            # moze biti None (reset) ili float
            self.locked_levels[symbol]["base_price"] = base_price

        if open_time is not _UNCHANGED:
            # moze biti None (reset) ili float(timestamp)
            self.locked_levels[symbol]["open_time"] = open_time

        self.save_state()
    # -----------------------------------------------

    def setup(self):
        """Inicijalizacija."""
        start_msg = f"🚀 <b>BOT STARTED (v23 - BasePrice Persist)</b>\nSimboli: {self.cfg.symbols}\nHistory: 50 (Fix)\nSleep: 2s"
        logger.info(start_msg.replace("<b>", "").replace("</b>", "").replace("\n", " "))
        self.tg.send(start_msg)
        
        try:
            logger.info("⏳ Preuzimam pravila sa berze...")
            exchange_info = self.client.futures_exchange_info()
            
            for target_symbol in self.cfg.symbols:
                self.last_trade_time[target_symbol] = 0
                self.last_close_time[target_symbol] = 0
                
                try:
                    self.client.futures_change_leverage(symbol=target_symbol, leverage=self.cfg.leverage)
                except BinanceAPIException: pass 

                try:
                    self.client.futures_change_margin_type(symbol=target_symbol, marginType=self.cfg.margin_type)
                except BinanceAPIException: pass

                for s in exchange_info['symbols']:
                    if s['symbol'] == target_symbol:
                        step_size = float([f['stepSize'] for f in s['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                        self.rules[target_symbol] = {
                            'qty_prec': int(s['quantityPrecision']),
                            'step_size': step_size
                        }
                        logger.info(f"✅ {target_symbol}: Ready")
                        break
        except Exception as e:
            logger.error(f"Setup Error: {e}")

    def get_market_price(self, symbol):
        ticker = self.client.futures_symbol_ticker(symbol=symbol)
        return float(ticker['price'])
    
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
        except:
            return None

    def get_real_base_price(self, symbol, avg_price, position_amt):
        """NOVA METODA: Prvo gleda fajl, pa onda racuna."""
        # 1. Proveri fajl
        state = self.locked_levels.get(symbol, {})
        stored_base = state.get("base_price")
        
        if stored_base is not None and stored_base > 0:
            return float(stored_base)
            
        # 2. Ako nema u fajlu, izracunaj matematicki (Fallback)
        math_base = self.calculate_base_math(symbol, avg_price, position_amt)
        
        # 3. Odmah snimi u fajl da sledeci put imamo
        if math_base:
            self.update_state(symbol, base_price=math_base)
            
        return math_base

    def get_active_position(self, symbol):
        positions = self.client.futures_position_information(symbol=symbol)
        for p in positions:
            side = p.get('positionSide', 'BOTH')
            if self.cfg.hedge_mode and side == 'LONG': return p
            elif not self.cfg.hedge_mode and side == 'BOTH': return p
        return None

    def get_fear_and_greed(self):
        if time.time() - self.fng_last_update < 3600: return self.fng_value
        try:
            url = "https://api.alternative.me/fng/"
            r = requests.get(url, timeout=10).json()
            self.fng_value = int(r['data'][0]['value'])
            self.fng_last_update = time.time()
            return self.fng_value
        except: return self.fng_value

    def check_entry_signal(self, symbol, current_price):
        try:
            if self.cfg.fng_enabled:
                if self.get_fear_and_greed() > self.cfg.fng_max: return False
            
            # Uzimamo lookback + 1 svecu (zadnju ignorisemo kao "tekuca")
            klines = self.client.futures_klines(symbol=symbol, interval='1m', limit=self.cfg.lookback + 1)
            lows = [float(k[3]) for k in klines[:-1]]
            if current_price <= min(lows): return True
            return False
        except: return False

    def smart_round(self, symbol, quantity):
        rule = self.rules.get(symbol)
        if not rule: return quantity
        step = rule['step_size']
        precision = rule['qty_prec']
        qty = int(quantity / step) * step
        return float(f"{qty:.{precision}f}")

    def execute_trade(self, symbol, side, quantity, reason=""):
        try:
            price = self.get_market_price(symbol)
            if quantity * price < 5: return False

            logger.info(f"⚡ {symbol}: {side} {quantity} ({reason})")
            
            order_params = {
                'symbol': symbol, 'side': side, 'type': 'MARKET', 'quantity': quantity
            }
            if self.cfg.hedge_mode: order_params['positionSide'] = 'LONG'

            self.client.futures_create_order(**order_params)
            
            if side == 'BUY':
                self.last_trade_time[symbol] = time.time() 
            elif side == 'SELL':
                self.last_close_time[symbol] = time.time() 
            
            msg = f"{'✅' if side=='BUY' else '💰'} <b>{symbol}</b>\nAkcija: {side}\nKoličina: {quantity}\nRazlog: {reason}\nCena: {price}"
            self.tg.send(msg)
            return True
        except Exception as e:
            logger.error(f"Trade Error {symbol}: {e}")
            return False

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
                    print(f"🔹 {symbol} [LEVEL {lock_level}]: Nema pozicije | Cena: {price:.4f}")
                    continue

                amt = float(pos['positionAmt'])
                entry = float(pos['entryPrice'])  # Binance avg entry
                size = abs(amt) * entry

                # PosLevel (heuristika) je samo informativan
                pos_level = self.get_dca_level(size)

                # Baza (anchor) za drop-ove (iz state-a)
                base_price = self.get_real_base_price(symbol, entry, amt)
                if base_price is None:
                    base_price = entry

                # Take-profit target se računa po LOCK levelu (state je autoritativan)
                p_idx = min(lock_level, len(self.cfg.profits)) - 1
                if p_idx < 0:
                    p_idx = 0
                target = entry * (1 + (self.cfg.profits[p_idx] / 100))

                # Sledeći dokup: po LOCK levelu (drop index = lock_level; kupovina = lock_level -> lock_level+1)
                buy_txt = "MAX"
                if lock_level < len(self.cfg.drops):
                    drop_pct = self.cfg.drops[lock_level]
                    next_price = base_price * (1 - (drop_pct / 100))
                    buy_txt = f"{next_price:.4f}$ (-{drop_pct}%) [Base {base_price:.4f}]"

                pos_txt = f"Pos {pos_level}"
                if abs(lock_level - pos_level) >= 2:
                    pos_txt = f"⚠️Pos {pos_level}"

                print(
                    f"🔸 {symbol} [LEVEL {lock_level} | {pos_txt}]: "
                    f"Size {size:.1f}$ | Avg {entry:.4f} | TP {target:.4f} | Next DCA {buy_txt}"
                )
            except Exception:
                pass
        print("----------------------------------------------\n")
        self.save_state()
    def process_symbol(self, symbol):
        try:
            pos_data = self.get_active_position(symbol)
            price = self.get_market_price(symbol)
            
            if pos_data is None: pos_amt = 0.0; entry_price = 0.0
            else:
                pos_amt = float(pos_data['positionAmt'])
                entry_price = float(pos_data['entryPrice'])

            # --- 1. NEMA POZICIJE (ULAZ) ---
            if pos_amt == 0:
                # Resetujemo state kad nema pozicije (level ili base_price ne smeju da ostanu od prosle kampanje)
                current_stored = self.locked_levels.get(symbol, {})
                if current_stored.get("level", 0) != 0 or current_stored.get("base_price") is not None:
                    self.update_state(symbol, level=0, base_price=None, open_time=None)

                last_close = self.last_close_time.get(symbol, 0)
                if (time.time() - last_close) / 60 < self.cfg.entry_cooldown_min: return

                if self.check_entry_signal(symbol, price):
                    qty = self.smart_round(symbol, self.cfg.base_order_size / price)
                    if self.execute_trade(symbol, 'BUY', qty, "START - Entry"):
                        # UPISUJEMO STARTNU (FILL) CENU U FAJL.
                        # get_market_price() nije garantovana fill cena, zato citamo entryPrice sa berze posle ordera.
                        base_price = price
                        for _ in range(4):
                            time.sleep(0.25)
                            p = self.get_active_position(symbol)
                            if p and float(p.get("positionAmt", 0)) != 0:
                                try:
                                    base_price = float(p["entryPrice"])
                                    break
                                except:  # noqa: E722
                                    pass
                        self.update_state(symbol, level=0, base_price=base_price, open_time=time.time())

            
            # --- 2. IMA POZICIJE (MENADŽMENT) ---
            else:
                current_notional = abs(pos_amt) * entry_price
                if current_notional < 5: return 

                                # Level iz stvarne pozicije (heuristika) - samo za INFO
                pos_level = self.get_dca_level(current_notional)

                # State je "sveto pismo" za DCA korake
                stored = self.locked_levels.get(symbol, {})
                try:
                    state_level = int(stored.get("level", 0))
                except Exception:
                    state_level = 0

                current_level = state_level

                # Opcioni warning ako se pozicija i state razilaze
                # Rate-limit mismatch warning (log once per change, or every 10 minutes)
                if abs(state_level - pos_level) >= 2:
                    now_ts = time.time()
                    prev = getattr(self, "_mismatch_warn_cache", {}).get(symbol)
                    # Default: log only once per (PosLevel, LockLevel) pair.
                    # Optional: set extra.mismatchWarnEverySec in config to re-log periodically.
                    warn_every = float(self.cfg.data.get("extra", {}).get("mismatchWarnEverySec", 0) or 0)
                    should_log = (
                        prev is None
                        or prev[0] != pos_level
                        or prev[1] != state_level
                        or (warn_every > 0 and (now_ts - prev[2]) >= warn_every)
                    )
                    if should_log:
                        logger.warning(
                            f"⚠️ {symbol}: PosLevel={pos_level} se razlikuje od LockLevel={state_level}. "
                            f"State je autoritativan; DCA će nastaviti po state-u."
                        )
                        self._mismatch_warn_cache[symbol] = (pos_level, state_level, now_ts)

                # Ovde osiguravamo da je base_price upisan u fajl (za stare pozicije)
                # Poziv ove funkcije ce automatski snimiti base ako fali
                real_base_price = self.get_real_base_price(symbol, entry_price, pos_amt)
                if real_base_price is None: real_base_price = entry_price

                # ===== FORCE CLOSE (time + drawdown vs avg entry) =====
                try:
                    fc_days = float(self.cfg.data.get("extra", {}).get("forceCloseDays", 0) or 0)
                    fc_pct  = float(self.cfg.data.get("extra", {}).get("forceClosePercent", 0) or 0)

                    if fc_days > 0 and fc_pct > 0:
                        st = self.locked_levels.get(symbol, {})
                        open_time = st.get("open_time")

                        # Fallback ako nemamo u state (stare pozicije)
                        if not open_time:
                            try:
                                open_time = float(pos_data.get("updateTime", 0)) / 1000.0
                            except:
                                open_time = None

                        if open_time:
                            days_open = (time.time() - float(open_time)) / 86400.0
                            dd_pct = ((entry_price - price) / entry_price) * 100.0  # entry_price je avg (Binance)
                            if days_open >= fc_days and dd_pct >= fc_pct:
                                if self.execute_trade(symbol, 'SELL', abs(pos_amt),
                                        f"FORCE CUT {days_open:.1f}d DD {dd_pct:.1f}% (avg {entry_price:.2f} -> {price:.2f})"):
                                    self.update_state(symbol, level=0, base_price=None, open_time=None)
                                return
                except:
                    pass
                # ================================================

                # A. PROFIT
                prof_idx = min(current_level, len(self.cfg.profits)) - 1
                if prof_idx < 0: prof_idx = 0
                target_pct = self.cfg.profits[prof_idx]
                take_profit_price = entry_price * (1 + (target_pct / 100))

                if price >= take_profit_price:
                    if self.execute_trade(symbol, 'SELL', abs(pos_amt), f"Take Profit (Level {current_level})"):
                        # Resetujemo state
                        self.update_state(symbol, level=0, base_price=None, open_time=None)
                    return 

                # B. DOKUP (DCA)
                if current_level < len(self.cfg.drops):
                    drop_pct = self.cfg.drops[current_level]
                    
                    # Kljucna promena: Koristimo real_base_price iz fajla!
                    dca_price = real_base_price * (1 - (drop_pct / 100))
                    
                    if price <= dca_price:
                        # Level Lock Check
                        target_level = current_level + 1
                        stored_lvl = self.locked_levels.get(symbol, {}).get("level", 0)
                        
                        if stored_lvl >= target_level: return 

                        # Cooldown Check
                        req_wait = 0
                        if current_level < len(self.cfg.dca_cooldowns_min):
                            req_wait = self.cfg.dca_cooldowns_min[current_level]
                        
                        last_trade = self.last_trade_time.get(symbol, 0)
                        if (time.time() - last_trade) / 60 < req_wait: return 

                        risk_factor = self.cfg.risks[current_level]
                        buy_value = self.cfg.base_order_size * risk_factor
                        qty = self.smart_round(symbol, buy_value / price)
                        
                        if self.execute_trade(symbol, 'BUY', qty, f"DCA Dokup (Level {current_level} -> {target_level}) [Base: {real_base_price:.2f}]"):
                            self.update_state(symbol, level=target_level)

        except Exception as e:
            logger.error(f"Greska {symbol}: {e}")

    def run(self):
        self.setup()
        logger.info("🤖 Multi-Coin Bot je aktivan.")
        last_h, last_r = 0, 0
        while True:
            self.reload_state()
            for s in self.cfg.symbols:
                self.process_symbol(s)
                time.sleep(2) # PAUZA 2 SEKUNDE
            
            if time.time() - last_h > 300:
                print(f"💓 Heartbeat...")
                last_h = time.time()
            
            if time.time() - last_r > 3600:
                self.report_status()
                last_r = time.time()
            
            time.sleep(2) # PAUZA 2 SEKUNDE

if __name__ == "__main__":
    bot = MultiCoinBot()
    bot.run()