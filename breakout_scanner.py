#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║                  BREAKOUTBOT — BREAKOUT SCANNER v1.0              ║
║                                                                    ║
║  Escanea los gainers del día (Yahoo Finance screener) y detecta   ║
║  cuando alguno rompe su ORB15 (máximo de los primeros 15 minutos  ║
║  de sesión) — descartando la señal si el precio YA había roto el  ║
║  PDH (máximo del día anterior) antes de esa ruptura: backtest de  ║
║  8 semanas mostró que esos casos rinden mucho peor (+0.6%/51%     ║
║  win a cierre) que cuando el PDH sigue intacto (+6.0%/79% win) —  ║
║  más "recorrido libre" hasta la siguiente resistencia obvia.      ║
║  Simula una compra en el momento del breakout y registra la       ║
║  entrada en outcomes.json para que outcome_tracker.py mida el     ║
║  resultado real con el tiempo (checkpoints de                     ║
║  15min/30min/1h/2h/cierre de sesión).                             ║
║                                                                    ║
║  Pensado para ejecutarse en pasadas cortas y repetidas (cada      ║
║  ~60-90s) durante la sesión — cada invocación es una pasada única ║
║  y stateless salvo por state.json (estado persistente del día).   ║
║  100% autónomo — .env, estado y logs propios, sin depender de     ║
║  ningún otro proyecto.                                            ║
╚══════════════════════════════════════════════════════════════════╝

SETUP:
  1. pip install -r requirements.txt
  2. .env en este mismo directorio con TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
  3. Ejecución manual (una pasada, escanea + alerta de breakouts nuevos):
       python3 breakout_scanner.py --telegram
"""

import os
import json
import time
import logging
import logging.handlers
import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

ET = ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("breakoutbot")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.handlers.RotatingFileHandler(
        Path(__file__).parent / "breakout_scan.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)
    return logger


log = setup_logging()


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))
    request_timeout: int = 12
    # Universo de gainers — umbral bajo a propósito: el breakout en sí es
    # la señal real, no queremos filtrar candidatos que aún no han subido
    # mucho pero están a punto de romper su ORB15.
    gain_min_pct: float = 3.0
    cap_min: int = 3_000_000
    cap_max: int = 150_000_000
    exchanges: tuple = ("NMS", "NYQ", "NGM", "NCM", "ASE")
    gainers_refresh_s: int = 300
    # Horario de sesión regular US, en hora de Nueva York (zoneinfo, DST-safe)
    market_open: str = "09:30"
    orb_end: str = "09:45"
    market_close: str = "16:00"
    checkpoint_minutes: dict = field(
        default_factory=lambda: {"m15": 15, "m30": 30, "h1": 60, "h2": 120}
    )


cfg = Config()

STATE_FILE = Path(__file__).parent / "state.json"
OUTCOMES_FILE = Path(__file__).parent / "outcomes.json"
OUTCOMES_LOCK_FILE = Path(__file__).parent / "outcomes.lock"


# ─────────────────────────────────────────────────────────────────
# TIEMPO (America/New_York, DST-safe vía zoneinfo)
# ─────────────────────────────────────────────────────────────────
def now_et() -> datetime:
    return datetime.now(ET)


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def orb_window_complete(dt: datetime) -> bool:
    return _hm(dt) >= cfg.orb_end


# ─────────────────────────────────────────────────────────────────
# MÓDULO: GAINERS DEL DÍA (Yahoo Finance screener)
# ─────────────────────────────────────────────────────────────────
def get_today_gainers() -> list[dict]:
    q = yf.EquityQuery("and", [
        yf.EquityQuery("is-in", ["exchange", *cfg.exchanges]),
        yf.EquityQuery("btwn", ["intradaymarketcap", cfg.cap_min, cfg.cap_max]),
        yf.EquityQuery("gt", ["percentchange", cfg.gain_min_pct]),
    ])
    try:
        res = yf.screen(q, count=100, sortField="percentchange", sortAsc=False)
    except Exception as e:
        log.error(f"[GAINERS] Error consultando screener: {e}")
        return []

    quotes = res.get("quotes", [])
    out = []
    for qd in quotes:
        ticker = qd.get("symbol")
        if not ticker:
            continue
        out.append({
            "ticker": ticker,
            "name": qd.get("shortName") or qd.get("longName") or "",
            "pct_change": round(qd.get("regularMarketChangePercent", 0), 1),
            "market_cap": qd.get("marketCap", 0),
        })
    log.info(f"[GAINERS] {len(out)} candidatos con subida >{cfg.gain_min_pct}% hoy")
    return out


# ─────────────────────────────────────────────────────────────────
# MÓDULO: PRECIOS — ORB15, PDH, última vela 5m completada, precio en vivo
# ─────────────────────────────────────────────────────────────────
def get_orb15(ticker: str) -> float | None:
    """Máximo de las velas de 5m entre market_open y orb_end (primeros 15
    minutos de sesión) — solo tiene sentido llamarla una vez esa ventana
    ha cerrado del todo (orb_window_complete)."""
    try:
        df = yf.Ticker(ticker).history(period="1d", interval="5m")
    except Exception as e:
        log.debug(f"[ORB15] Error descargando 5m de {ticker}: {e}")
        return None
    if df.empty:
        return None
    df.index = df.index.tz_convert(ET)
    window = df.between_time(cfg.market_open, cfg.orb_end, inclusive="left")
    if window.empty:
        return None
    return round(float(window["High"].max()), 4)


def get_pdh(ticker: str) -> float | None:
    """Máximo del día de sesión anterior (excluye la vela de hoy, que
    puede aparecer parcial en la descarga diaria)."""
    try:
        df = yf.Ticker(ticker).history(period="5d", interval="1d")
    except Exception as e:
        log.debug(f"[PDH] Error descargando diario de {ticker}: {e}")
        return None
    if df.empty:
        return None
    today_str = now_et().strftime("%Y-%m-%d")
    df = df[df.index.strftime("%Y-%m-%d") != today_str]
    if df.empty:
        return None
    return round(float(df["High"].iloc[-1]), 4)


def get_latest_completed_close(ticker: str) -> tuple[float, datetime] | None:
    """Cierre de la última vela de 5m YA completada (posterior a la
    ventana ORB) — se descarta la vela en formación para no disparar el
    breakout con un precio intrabar todavía provisional."""
    try:
        df = yf.Ticker(ticker).history(period="1d", interval="5m")
    except Exception as e:
        log.debug(f"[BAR] Error descargando 5m de {ticker}: {e}")
        return None
    if df.empty:
        return None
    df.index = df.index.tz_convert(ET)
    now = now_et()
    post_orb = df[df.index.strftime("%H:%M") >= cfg.orb_end]
    completed = post_orb[(post_orb.index + timedelta(minutes=5)) <= now]
    if completed.empty:
        return None
    last = completed.iloc[-1]
    return float(last["Close"]), completed.index[-1].to_pydatetime()


def get_live_price(ticker: str) -> float | None:
    try:
        fi = yf.Ticker(ticker).fast_info
        p = fi.get("lastPrice")
        return float(p) if p else None
    except Exception as e:
        log.debug(f"[PRICE] Error obteniendo precio de {ticker}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# MÓDULO: ESTADO PERSISTENTE DEL DÍA (state.json)
#
# Estados por ticker: "watching" (candidato, aún no rompe ORB15) →
# "entered" (breakout confirmado, entrada simulada registrada). Se
# reinicia cada día de sesión — el estado de ayer no es relevante hoy.
# ─────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("[STATE] Archivo corrupto, se reinicia")
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _fresh_state_for_today() -> dict:
    return {"date": now_et().strftime("%Y-%m-%d"), "gainers_last_refresh": None, "tickers": {}}


# ─────────────────────────────────────────────────────────────────
# MÓDULO: OUTCOMES (registro de entradas simuladas)
#
# outcomes.json lo escriben este script (al confirmar un breakout) y
# outcome_tracker.py (al resolver checkpoints) de forma independiente —
# el lock evita que un solape entre pasadas pise cambios del otro.
# ─────────────────────────────────────────────────────────────────
@contextmanager
def _outcomes_lock():
    import fcntl
    with open(OUTCOMES_LOCK_FILE, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def _load_outcomes() -> list[dict]:
    if OUTCOMES_FILE.exists():
        try:
            return json.loads(OUTCOMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("[OUTCOMES] Archivo corrupto, se ignora")
    return []


def _save_outcomes(outcomes: list[dict]) -> None:
    OUTCOMES_FILE.write_text(json.dumps(outcomes, indent=2, default=str), encoding="utf-8")


def record_outcome_entry(t: dict) -> None:
    entry_dt = now_et()
    close_dt = entry_dt.replace(hour=16, minute=0, second=0, microsecond=0)
    if close_dt <= entry_dt:
        close_dt = entry_dt + timedelta(minutes=1)  # margen si ya son las 16:00 en punto
    checkpoint_times = {k: (entry_dt + timedelta(minutes=m)).isoformat()
                         for k, m in cfg.checkpoint_minutes.items()}
    checkpoint_times["close"] = close_dt.isoformat()

    with _outcomes_lock():
        outcomes = _load_outcomes()
        outcomes.append({
            "ticker": t["ticker"],
            "entry_datetime": entry_dt.isoformat(),
            "entry_price": t["entry_price"],
            "orb15": t["orb15"],
            "pdh": t.get("pdh"),  # siempre intacto en este punto — ver filtro en run_once()
            "pct_change_seen": t.get("pct_change_seen"),
            "checkpoints": {k: None for k in list(cfg.checkpoint_minutes.keys()) + ["close"]},
            "checkpoint_times": checkpoint_times,
            "resolved": False,
        })
        _save_outcomes(outcomes)
    log.info(f"[OUTCOMES] {t['ticker']} registrado — entrada ${_fmt_price(t['entry_price'])} "
              f"a las {entry_dt.strftime('%H:%M:%S')} ET")


# ─────────────────────────────────────────────────────────────────
# MÓDULO: TELEGRAM
# ─────────────────────────────────────────────────────────────────
TELEGRAM_MAX_LEN = 4000

def _split_message(message: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(message) <= max_len:
        return [message]
    chunks, current = [], ""
    for block in message.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            current = block[:max_len] if len(block) > max_len else block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(message: str) -> bool:
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        log.warning("[TELEGRAM] Sin credenciales, se omite el envío")
        return False
    chunks = _split_message(message)
    ok = True
    for chunk in chunks:
        ok = _send_telegram_chunk(chunk) and ok
        if len(chunks) > 1:
            time.sleep(0.5)
    return ok


def _send_telegram_chunk(message: str) -> bool:
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": cfg.telegram_chat_id, "text": message,
            "parse_mode": "Markdown", "disable_web_page_preview": True,
        }, timeout=cfg.request_timeout)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"[TELEGRAM] Error enviando mensaje: {e}")
        return False


def _fmt_price(p: float) -> str:
    if p >= 1:
        return f"{p:.2f}"
    s = f"{p:.4f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return s


def build_breakout_alert(entries: list[dict]) -> str:
    today = now_et().strftime("%Y-%m-%d %H:%M")
    lines = [f"🚀 *Breakoutbot — Breakout detectado* · {today} ET\n"]
    for t in entries:
        # El PDH aquí siempre está intacto (si no, run_once() ya ha
        # descartado la señal) — se muestra como dato de "recorrido libre"
        # hasta la siguiente resistencia obvia, no como confirmación.
        pdh_line = f"\n  PDH intacto: ${_fmt_price(t['pdh'])} (todavía por delante)" if t.get("pdh") else ""
        pct = t.get("pct_change_seen")
        pct_line = f" ({pct:+.1f}% hoy)" if pct is not None else ""
        lines.append(
            f"*{t['ticker']}*{pct_line} rompe ORB15 (${_fmt_price(t['orb15'])}) "
            f"→ entrada simulada ${_fmt_price(t['entry_price'])}"
            f"{pdh_line}"
        )
    return "\n\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# MAIN — una pasada
# ─────────────────────────────────────────────────────────────────
def run_once(send_tg: bool = False):
    now = now_et()
    hm = _hm(now)
    log.info(f"[MAIN] Pasada a las {now.strftime('%H:%M:%S')} ET")

    if hm < cfg.market_open or hm > cfg.market_close:
        log.info(f"[MAIN] Fuera de horario de mercado ({hm} ET) — no hay nada que hacer")
        return

    state = _load_state()
    if state.get("date") != now.strftime("%Y-%m-%d"):
        log.info("[MAIN] Nuevo día de sesión — reiniciando estado")
        state = _fresh_state_for_today()

    # Refrescar universo de gainers cada cfg.gainers_refresh_s segundos —
    # no en cada pasada, para no saturar el screener con llamadas cada 75s.
    last_refresh = state.get("gainers_last_refresh")
    need_refresh = True
    if last_refresh:
        try:
            need_refresh = (now - datetime.fromisoformat(last_refresh)) > timedelta(seconds=cfg.gainers_refresh_s)
        except Exception:
            need_refresh = True
    if need_refresh:
        for g in get_today_gainers():
            t = g["ticker"]
            if t not in state["tickers"]:
                state["tickers"][t] = {
                    "status": "watching", "orb15": None, "pdh": None,
                    "pct_change_seen": g["pct_change"], "first_seen": now.isoformat(),
                }
        state["gainers_last_refresh"] = now.isoformat()
    else:
        log.debug("[MAIN] Caché de gainers aún fresca, no se refresca")

    watching = [t for t, e in state["tickers"].items() if e["status"] == "watching"]
    log.info(f"[MAIN] {len(watching)} ticker(s) en watching")

    orb_ready = orb_window_complete(now)
    new_entries = []
    for ticker in watching:
        entry = state["tickers"][ticker]
        if entry.get("pdh") is None:
            entry["pdh"] = get_pdh(ticker)
        if not orb_ready:
            continue  # ORB15 aún no se puede calcular (ventana 9:30-9:45 sin cerrar)
        if entry.get("orb15") is None:
            entry["orb15"] = get_orb15(ticker)
            if entry["orb15"] is None:
                continue
        bar = get_latest_completed_close(ticker)
        if bar is None:
            continue
        close_price, bar_time = bar
        if close_price <= entry["orb15"]:
            continue

        pdh = entry.get("pdh")

        # Filtro validado por backtest (8 semanas, 145 señales): si el PDH
        # ya estaba roto ANTES de esta ruptura de ORB15, el precio llega
        # más "extendido" y rinde mucho peor (+0.6%/51% win a cierre) que
        # cuando el PDH sigue intacto (+6.0%/79% win) — se descarta.
        # OJO: la comprobación usa close_price (el cierre de la misma vela
        # que confirma el breakout), no un precio en vivo posterior — es
        # EXACTAMENTE la misma definición de "pdh_confirmed" que usó el
        # backtest (backtest_breakout.py). Comparar contra un precio más
        # tardío (fast_info, potencialmente minutos después) mediría una
        # condición distinta a la que de verdad se validó.
        if pdh and close_price > pdh:
            entry["status"] = "rejected"
            entry["rejected_reason"] = "orb15_break_pero_pdh_ya_roto"
            log.info(f"[MAIN] {ticker} rompe ORB15 (${_fmt_price(entry['orb15'])}) pero ya había roto "
                      f"el PDH (${_fmt_price(pdh)}) — se descarta (peor rendimiento en backtest)")
            continue

        # Breakout confirmado — precio de entrada en vivo (más preciso que
        # el cierre de la vela para el precio de ENTRADA en sí, aunque la
        # condición de disparo arriba use el cierre de la vela).
        live_price = get_live_price(ticker) or close_price
        entry["status"] = "entered"
        entry["entered_at"] = now.isoformat()
        new_entries.append({
            "ticker": ticker, "entry_price": live_price, "orb15": entry["orb15"],
            "pdh": pdh, "pct_change_seen": entry.get("pct_change_seen"),
            "bar_close": close_price, "bar_time": bar_time.isoformat(),
        })
        time.sleep(0.2)

    _save_state(state)

    if new_entries:
        for t in new_entries:
            record_outcome_entry(t)
        log.info(f"[MAIN] {len(new_entries)} breakout(s) nuevo(s): " +
                  ", ".join(t["ticker"] for t in new_entries))
        if send_tg:
            if send_telegram(build_breakout_alert(new_entries)):
                log.info("[TELEGRAM] Alerta de breakout enviada")
    else:
        log.info("[MAIN] Sin breakouts nuevos en esta pasada")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Enviar alertas por Telegram")
    args = parser.parse_args()
    run_once(send_tg=args.telegram)
