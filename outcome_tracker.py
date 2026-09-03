#!/usr/bin/env python3
"""
BREAKOUTBOT — Outcome Tracker

Rellena los checkpoints (15min/30min/1h/2h/cierre de sesión) de
outcomes.json a medida que vencen, calculando el retorno real (long —
compra simulada en el breakout) de cada señal registrada por
breakout_scanner.py. Es el backtest continuo del propio sistema.

Uso:
  python3 outcome_tracker.py                # rellena checkpoints vencidos
  python3 outcome_tracker.py --telegram      # + manda resumen de entradas
                                                recién completadas (close)
"""

import os
import logging
import logging.handlers
import argparse
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

logger = logging.getLogger("outcome_tracker")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = logging.handlers.RotatingFileHandler(
    Path(__file__).parent / "outcome_tracker.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
logger.addHandler(_ch)

OUTCOMES_FILE = Path(__file__).parent / "outcomes.json"
OUTCOMES_LOCK_FILE = Path(__file__).parent / "outcomes.lock"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECKPOINT_ORDER = ["m15", "m30", "h1", "h2", "close"]
STALE_CHECKPOINT_GIVEUP_HOURS = 48  # si lleva vencido más de esto sin poder
                                     # obtener precio, se marca "N/A" en vez
                                     # de reintentar para siempre (ticker
                                     # deslistado, sin liquidez, etc.)


@contextmanager
def outcomes_lock():
    """Mismo archivo de lock que breakout_scanner.py — ambos scripts leen
    y escriben outcomes.json de forma independiente; sin esto, un solape
    entre pasadas podría pisar los cambios del otro."""
    import fcntl
    with open(OUTCOMES_LOCK_FILE, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def load_outcomes() -> list[dict]:
    if OUTCOMES_FILE.exists():
        try:
            return json.loads(OUTCOMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[OUTCOMES] Archivo corrupto, se ignora")
    return []


def save_outcomes(outcomes: list[dict]) -> None:
    OUTCOMES_FILE.write_text(json.dumps(outcomes, indent=2, default=str), encoding="utf-8")


def fmt_price(p: float) -> str:
    if p >= 1:
        return f"{p:.2f}"
    s = f"{p:.4f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def get_last_price(ticker: str) -> float | None:
    try:
        fi = yf.Ticker(ticker).fast_info
        p = fi.get("lastPrice")
        return float(p) if p else None
    except Exception as e:
        logger.debug(f"[PRICE] Error obteniendo precio de {ticker}: {e}")
        return None


def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("[TELEGRAM] Sin credenciales, se omite el envío")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": message,
            "parse_mode": "Markdown", "disable_web_page_preview": True,
        }, timeout=12)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"[TELEGRAM] Error enviando mensaje: {e}")
        return False


def update_checkpoints(outcomes: list[dict]) -> list[dict]:
    """Para cada entrada sin resolver, rellena los checkpoints cuya fecha
    ya venció (long: retorno = (precio_hoy - entrada)/entrada, ya que es
    una compra simulada en el breakout alcista)."""
    newly_completed = []

    for o in outcomes:
        if o["resolved"]:
            continue
        entry_price = o["entry_price"]
        changed = False
        for key in CHECKPOINT_ORDER:
            if o["checkpoints"].get(key) is not None:
                continue
            target_date_str = o["checkpoint_times"].get(key)
            if not target_date_str:
                continue
            target_date = datetime.fromisoformat(target_date_str)
            now = datetime.now(target_date.tzinfo) if target_date.tzinfo else datetime.now()
            if now < target_date:
                continue
            price = get_last_price(o["ticker"])
            if price is None:
                hours_overdue = (now - target_date).total_seconds() / 3600
                if hours_overdue >= STALE_CHECKPOINT_GIVEUP_HOURS:
                    o["checkpoints"][key] = "N/A"
                    changed = True
                    logger.warning(f"[TRACKER] {o['ticker']} {key}: sin precio tras "
                                    f"{hours_overdue:.0f}h vencido, se marca N/A")
                else:
                    logger.warning(f"[TRACKER] {o['ticker']} {key}: sin precio, se reintenta en la próxima ejecución")
                continue
            long_return_pct = (price - entry_price) / entry_price * 100
            o["checkpoints"][key] = round(long_return_pct, 2)
            changed = True
            logger.info(f"[TRACKER] {o['ticker']} {key}: ${fmt_price(entry_price)} → "
                        f"${fmt_price(price)} → retorno {long_return_pct:+.1f}%")

        if changed and all(o["checkpoints"].get(k) is not None for k in CHECKPOINT_ORDER):
            o["resolved"] = True
            newly_completed.append(o)
            logger.info(f"[TRACKER] {o['ticker']} — todos los checkpoints completados, entrada cerrada")

    return newly_completed


def build_completion_message(completed: list[dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📈 *Breakoutbot — Resultado de señales* · {today}\n"]
    for o in completed:
        cps = " · ".join(f"{k}: {v:+.1f}%" if isinstance(v, (int, float)) else f"{k}: {v}"
                         for k, v in o["checkpoints"].items())
        pdh_line = f" · PDH: ${fmt_price(o['pdh'])}" if o.get("pdh") else ""
        lines.append(
            f"*{o['ticker']}* · entrada ${fmt_price(o['entry_price'])} el {o['entry_datetime'][:16].replace('T', ' ')}\n"
            f"  ORB15: ${fmt_price(o['orb15'])}{pdh_line}\n"
            f"  Retorno: {cps}"
        )
    return "\n\n".join(lines)


def run(send_tg: bool = False):
    logger.info("=" * 60)
    logger.info("BREAKOUTBOT — Outcome Tracker")
    logger.info("=" * 60)

    with outcomes_lock():
        outcomes = load_outcomes()
        if not outcomes:
            logger.info("[TRACKER] Sin entradas registradas todavía")
            return

        pending = sum(1 for o in outcomes if not o["resolved"])
        logger.info(f"[TRACKER] {len(outcomes)} entradas totales, {pending} pendientes de resolver")

        completed = update_checkpoints(outcomes)
        save_outcomes(outcomes)

    if completed:
        logger.info(f"[TRACKER] {len(completed)} entrada(s) completadas hoy: " +
                    ", ".join(o["ticker"] for o in completed))
        if send_tg:
            msg = build_completion_message(completed)
            if send_telegram(msg):
                logger.info("[TELEGRAM] Resumen de resultados enviado")

    resolved = [o for o in outcomes if o["resolved"]]
    if resolved:
        all_returns = [v for o in resolved for v in o["checkpoints"].values()
                        if isinstance(v, (int, float))]
        if all_returns:
            win_rate = 100 * sum(1 for v in all_returns if v > 0) / len(all_returns)
            logger.info(f"[STATS] {len(resolved)} entradas resueltas — "
                        f"retorno medio (todos los checkpoints): {sum(all_returns)/len(all_returns):.1f}% "
                        f"· win-rate: {win_rate:.0f}% (n={len(all_returns)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Enviar resumen por Telegram")
    args = parser.parse_args()
    run(send_tg=args.telegram)
