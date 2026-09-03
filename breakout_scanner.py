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
║  15min/30min/1h/2h/cierre de sesión). Cada pasada también vigila  ║
║  las posiciones ya abiertas y las cierra por stop-loss en cuanto  ║
║  el precio rompe el mínimo del ORB15 — no espera al siguiente     ║
║  checkpoint fijo para protegerlas. El tamaño de cada posición se  ║
║  calcula por riesgo (no un importe fijo igual para todas): cada   ║
║  trade arriesga como mucho risk_per_trade_pct% de la cuenta si    ║
║  salta el stop — sin stop calculable no hay tamaño protegido, y   ║
║  la señal se descarta.                                            ║
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
    # Position sizing por riesgo — cada trade arriesga como mucho
    # risk_per_trade_pct% de la cuenta SI SALTA EL STOP, no un importe fijo
    # igual para todos. Con distancias de stop tan distintas entre tickers
    # (visto en el backtest: desde ~1% hasta ~14% bajo la entrada), un
    # importe fijo por trade arriesgaría cantidades muy distintas sin
    # querer. Sin stop calculable no hay forma de tamañar con riesgo
    # conocido, así que esas señales se descartan (ver run_once()).
    # Se leen de account_config.json (fuente única compartida con
    # outcome_tracker.py y export_outcomes_excel.py) — estos valores por
    # defecto solo se usan si el archivo falta o está corrupto, para no
    # tumbar el scanner por un problema de configuración.
    starting_capital: float = field(default_factory=lambda: _load_account_config()["starting_capital"])
    risk_per_trade_pct: float = field(default_factory=lambda: _load_account_config()["risk_per_trade_pct"])


ACCOUNT_CONFIG_FILE = Path(__file__).parent / "account_config.json"
_ACCOUNT_CONFIG_DEFAULTS = {"starting_capital": 2500.0, "risk_per_trade_pct": 0.5}


def _load_account_config() -> dict:
    if ACCOUNT_CONFIG_FILE.exists():
        try:
            data = json.loads(ACCOUNT_CONFIG_FILE.read_text(encoding="utf-8"))
            return {**_ACCOUNT_CONFIG_DEFAULTS, **data}
        except Exception:
            log.warning("[CONFIG] account_config.json corrupto, usando valores por defecto")
    return dict(_ACCOUNT_CONFIG_DEFAULTS)


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
#
# LIMITACIÓN CONOCIDA (aceptada, no arreglable con datos gratuitos):
# yf.screen() no es una base de datos completa en tiempo real — es el
# índice parcial/cacheado que usa la propia web de Yahoo, con huecos de
# cobertura reales en microcaps poco líquidas. Verificado: NCPL subió
# +30% el 2 sep 2026 y cumplía todos los filtros (cap $8.08M, exchange
# NCM) pero nunca apareció en el screener ese día. No hay arreglo
# gratuito — requeriría un screener de pago (Polygon, IEX, etc.) con
# cobertura garantizada.
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
def get_orb_window(ticker: str) -> tuple[float, float] | None:
    """(orb_high, orb_low) de las velas de 5m entre market_open y orb_end
    (primeros 15 minutos de sesión) — solo tiene sentido llamarla una vez
    esa ventana ha cerrado del todo (orb_window_complete). orb_low se usa
    como nivel de stop-loss: si el precio vuelve a caer por debajo del
    mínimo de la apertura, la tesis del breakout queda invalidada."""
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
    return round(float(window["High"].max()), 4), round(float(window["Low"].min()), 4)


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


def get_current_equity(outcomes: list[dict]) -> float:
    """Capital inicial + P&L $ realizado de todas las entradas YA
    resueltas (cierre normal o stop-loss) — las que siguen abiertas no
    cuentan (mark-to-market no se contempla, solo P&L cerrado). Se usa
    para tamañar la SIGUIENTE entrada por riesgo, no las que ya están
    en marcha (esas ya quedaron tamañadas con la equity que había en
    su momento)."""
    equity = cfg.starting_capital
    for o in outcomes:
        if o.get("resolved") and o.get("position_value"):
            close_pct = o["checkpoints"].get("close")
            if isinstance(close_pct, (int, float)):
                equity += o["position_value"] * close_pct / 100
    return equity


def record_outcome_entry(t: dict) -> dict | None:
    """Registra la entrada con el tamaño de posición calculado por riesgo
    (no un importe fijo igual para todas): shares = riesgo_$ / distancia
    al stop, donde riesgo_$ = equity_actual * risk_per_trade_pct. Si esa
    cuenta sale a 0 acciones (distancia al stop demasiado grande para el
    riesgo permitido en esta cuenta), NO se registra la entrada — sin
    tamaño protegido no hay trade. Devuelve el sizing calculado (para la
    alerta de Telegram) o None si se descartó por tamaño 0."""
    entry_dt = now_et()
    close_dt = entry_dt.replace(hour=16, minute=0, second=0, microsecond=0)
    if close_dt <= entry_dt:
        close_dt = entry_dt + timedelta(minutes=1)  # margen si ya son las 16:00 en punto
    checkpoint_times = {k: (entry_dt + timedelta(minutes=m)).isoformat()
                         for k, m in cfg.checkpoint_minutes.items()}
    checkpoint_times["close"] = close_dt.isoformat()

    stop_price = t["orb_low"]  # run_once() ya garantiza que existe antes de llegar aquí
    stop_distance = t["entry_price"] - stop_price

    with _outcomes_lock():
        outcomes = _load_outcomes()
        equity = get_current_equity(outcomes)
        risk_amount = round(equity * cfg.risk_per_trade_pct / 100, 2)
        shares = int(risk_amount / stop_distance) if stop_distance > 0 else 0
        if shares <= 0:
            log.warning(f"[OUTCOMES] {t['ticker']}: distancia al stop (${stop_distance:.4f}) demasiado "
                        f"grande para el riesgo permitido (${risk_amount:.2f} de ${equity:.2f}) — 0 acciones, se descarta")
            return None
        position_value = round(shares * t["entry_price"], 2)

        outcomes.append({
            "ticker": t["ticker"],
            "entry_datetime": entry_dt.isoformat(),
            "entry_price": t["entry_price"],
            "orb15": t["orb15"],
            "pdh": t.get("pdh"),  # siempre intacto en este punto — ver filtro en run_once()
            "pct_change_seen": t.get("pct_change_seen"),
            "stop_price": stop_price,  # stop-loss = mínimo del ORB15
            "stopped_out": False,
            "stop_exit_price": None,
            "stop_exit_time": None,
            "account_equity_at_entry": round(equity, 2),
            "risk_amount": risk_amount,
            "shares": shares,
            "position_value": position_value,
            "checkpoints": {k: None for k in list(cfg.checkpoint_minutes.keys()) + ["close"]},
            "checkpoint_times": checkpoint_times,
            "resolved": False,
        })
        _save_outcomes(outcomes)
    log.info(f"[OUTCOMES] {t['ticker']} registrado — {shares} acciones × ${_fmt_price(t['entry_price'])} "
              f"(${position_value:,.2f}) a las {entry_dt.strftime('%H:%M:%S')} ET · "
              f"riesgo ${risk_amount:.2f} · stop ${_fmt_price(stop_price)} · equity ${equity:,.2f}")
    return {"shares": shares, "risk_amount": risk_amount, "position_value": position_value,
            "account_equity_at_entry": round(equity, 2)}


def apply_stop_loss(ticker: str, exit_price: float, now: datetime) -> dict | None:
    """Marca la entrada abierta de `ticker` como cerrada por stop-loss:
    rellena TODOS los checkpoints pendientes con el retorno del stop (una
    vez vendida la posición, el retorno en cualquier checkpoint posterior
    ya es ese, no depende de cómo siga moviéndose el precio). Devuelve
    {'stop_return', 'dollar_pnl'}, o None si no había ninguna entrada
    abierta hoy para ese ticker (no debería pasar, pero por si acaso)."""
    with _outcomes_lock():
        outcomes = _load_outcomes()
        today_str = now.strftime("%Y-%m-%d")
        target = None
        for o in outcomes:
            if (o["ticker"] == ticker and o["entry_datetime"][:10] == today_str
                    and not o.get("stopped_out") and not o.get("resolved")):
                target = o
        if target is None:
            log.warning(f"[STOP-LOSS] {ticker}: no se encontró una entrada abierta de hoy en outcomes.json")
            return None

        entry_price = target["entry_price"]
        stop_return = round((exit_price - entry_price) / entry_price * 100, 2)
        position_value = target.get("position_value")
        dollar_pnl = round(position_value * stop_return / 100, 2) if position_value else None
        target["stopped_out"] = True
        target["stop_exit_price"] = exit_price
        target["stop_exit_time"] = now.isoformat()
        for key in target["checkpoints"]:
            if target["checkpoints"][key] is None:
                target["checkpoints"][key] = stop_return
        target["resolved"] = all(v is not None for v in target["checkpoints"].values())
        _save_outcomes(outcomes)

    log.info(f"[STOP-LOSS] {ticker} vendido a ${_fmt_price(exit_price)} — retorno {stop_return:+.1f}%"
              + (f" (${dollar_pnl:+,.2f})" if dollar_pnl is not None else ""))
    return {"stop_return": stop_return, "dollar_pnl": dollar_pnl}


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
        stop_line = f"\n  🛑 Stop-loss: ${_fmt_price(t['orb_low'])} (mínimo del ORB15)"
        size_line = (f"\n  📐 {t['shares']} acciones (${t['position_value']:,.2f}) · "
                     f"riesgo ${t['risk_amount']:.2f} · equity ${t['account_equity_at_entry']:,.2f}"
                     if t.get("shares") else "")
        pct = t.get("pct_change_seen")
        pct_line = f" ({pct:+.1f}% hoy)" if pct is not None else ""
        lines.append(
            f"*{t['ticker']}*{pct_line} rompe ORB15 (${_fmt_price(t['orb15'])}) "
            f"→ entrada simulada ${_fmt_price(t['entry_price'])}"
            f"{pdh_line}"
            f"{stop_line}"
            f"{size_line}"
        )
    return "\n\n".join(lines)


def build_stop_loss_alert(stopped: list[dict]) -> str:
    today = now_et().strftime("%Y-%m-%d %H:%M")
    lines = [f"🛑 *Breakoutbot — Stop-loss disparado* · {today} ET\n"]
    for s in stopped:
        pnl_line = f" (${s['dollar_pnl']:+,.2f})" if s.get("dollar_pnl") is not None else ""
        lines.append(
            f"*{s['ticker']}* vendida a ${_fmt_price(s['exit_price'])} "
            f"(rompió el mínimo del ORB15, ${_fmt_price(s['stop_price'])}) "
            f"→ retorno {s['stop_return']:+.1f}%{pnl_line}"
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
                    "status": "watching", "orb15": None, "orb_low": None, "pdh": None,
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
            orb_window = get_orb_window(ticker)
            if orb_window is None:
                continue
            entry["orb15"], entry["orb_low"] = orb_window
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

        # Sin mínimo del ORB15 no hay forma de tamañar la posición por
        # riesgo conocido — sin stop calculable, no se protege la cuenta,
        # así que no se toma la señal (se descarta, no solo se avisa).
        if entry.get("orb_low") is None:
            entry["status"] = "rejected"
            entry["rejected_reason"] = "sin_stop_calculable"
            log.warning(f"[MAIN] {ticker} rompe ORB15 pero no se pudo calcular el mínimo del ORB15 "
                        f"(sin stop-loss no se puede proteger la posición) — se descarta")
            continue

        # Breakout confirmado — precio de entrada en vivo (más preciso que
        # el cierre de la vela para el precio de ENTRADA en sí, aunque la
        # condición de disparo arriba use el cierre de la vela). El estado
        # final ("entered" o "rejected" por tamaño 0) se decide más abajo,
        # tras calcular el sizing por riesgo en record_outcome_entry().
        live_price = get_live_price(ticker) or close_price
        new_entries.append({
            "ticker": ticker, "entry_price": live_price, "orb15": entry["orb15"],
            "orb_low": entry.get("orb_low"), "pdh": pdh, "pct_change_seen": entry.get("pct_change_seen"),
            "bar_close": close_price, "bar_time": bar_time.isoformat(),
        })
        time.sleep(0.2)

    # Vigilancia de stop-loss — posiciones ya "entered" se comprueban en
    # CADA pasada (no solo en los checkpoints fijos de outcome_tracker.py),
    # para poder salir en cuanto el precio rompe el mínimo del ORB15, no
    # solo cuando toque la hora del siguiente checkpoint.
    stopped = []
    entered_tickers = [t for t, e in state["tickers"].items() if e["status"] == "entered"]
    for ticker in entered_tickers:
        entry = state["tickers"][ticker]
        stop_price = entry.get("orb_low")
        if stop_price is None:
            continue  # sin nivel de stop calculado — no se puede proteger esta posición
        live_price = get_live_price(ticker)
        if live_price is None:
            continue
        if live_price <= stop_price:
            result = apply_stop_loss(ticker, live_price, now)
            if result is not None:
                entry["status"] = "stopped"
                stopped.append({"ticker": ticker, "exit_price": live_price, "stop_price": stop_price,
                                 "stop_return": result["stop_return"], "dollar_pnl": result["dollar_pnl"]})
        time.sleep(0.2)

    # Resolver cada breakout candidato: record_outcome_entry() calcula el
    # tamaño de posición por riesgo (equity actual × risk_per_trade_pct) y
    # devuelve None si sale a 0 acciones (distancia al stop demasiado
    # grande para el riesgo permitido) — esas se marcan "rejected", no
    # "entered", y no generan alerta de entrada.
    confirmed_entries = []
    for t in new_entries:
        sizing = record_outcome_entry(t)
        if sizing is not None:
            confirmed_entries.append({**t, **sizing})
            state["tickers"][t["ticker"]]["status"] = "entered"
            state["tickers"][t["ticker"]]["entered_at"] = now.isoformat()
        else:
            state["tickers"][t["ticker"]]["status"] = "rejected"
            state["tickers"][t["ticker"]]["rejected_reason"] = "position_too_small"

    _save_state(state)

    if confirmed_entries:
        log.info(f"[MAIN] {len(confirmed_entries)} breakout(s) nuevo(s): " +
                  ", ".join(t["ticker"] for t in confirmed_entries))
        if send_tg:
            if send_telegram(build_breakout_alert(confirmed_entries)):
                log.info("[TELEGRAM] Alerta de breakout enviada")
    if stopped:
        log.info(f"[MAIN] {len(stopped)} stop-loss disparado(s): " +
                  ", ".join(f"{s['ticker']} ({s['stop_return']:+.1f}%)" for s in stopped))
        if send_tg:
            if send_telegram(build_stop_loss_alert(stopped)):
                log.info("[TELEGRAM] Alerta de stop-loss enviada")
    if not confirmed_entries and not stopped:
        log.info("[MAIN] Sin breakouts nuevos ni stops disparados en esta pasada")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Enviar alertas por Telegram")
    args = parser.parse_args()
    run_once(send_tg=args.telegram)
