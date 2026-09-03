#!/usr/bin/env python3
"""
BREAKOUTBOT — Backtest histórico puntual (semana pasada)

Script exploratorio, NO forma parte de los crons de producción. El
screener en vivo de Yahoo (yf.screen) no admite fecha pasada, así que
reconstruye "quién fue gainer" cada día de la semana pasada a partir de
un universo amplio de tickers micro/small cap activos HOY (sesgo: el
universo es el de hoy, no el exacto de la semana pasada). Para cada
(ticker, día) con subida diaria > GAIN_MIN_PCT aplica la misma lógica de
producción — ORB15 + confirmación PDH sobre velas de 5 minutos de ESE
día concreto (dentro de la ventana de 60 días que permite yfinance) — y
mide el retorno real a 15min/30min/1h/2h/cierre de sesión.

Uso:
  python3 backtest_breakout.py
"""
import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")

GAIN_MIN_PCT = 3.0
CAP_MIN = 3_000_000
CAP_MAX = 150_000_000
EXCHANGES = ("NMS", "NYQ", "NGM", "NCM", "ASE")
UNIVERSE_COUNT = 250
MARKET_OPEN = "09:30"
ORB_END = "09:45"
CHECKPOINT_MINUTES = {"m15": 15, "m30": 30, "h1": 60, "h2": 120}
# yfinance solo da velas de 5m de los últimos 60 días — con margen de
# seguridad (checkout/setup, festivos) para no pedir justo el límite.
MAX_WEEKS_5M = 8

OUT_FILE = Path(__file__).parent / "backtest_results.json"


def historical_range(weeks: int) -> list:
    """Días laborables de las últimas `weeks` semanas completas, sin
    incluir la semana actual (aún en curso, no comparable)."""
    today = datetime.now(ET).date()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=weeks)
    days, d = [], start
    while d < this_monday:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def get_broad_universe() -> list[str]:
    q = yf.EquityQuery("and", [
        yf.EquityQuery("is-in", ["exchange", *EXCHANGES]),
        yf.EquityQuery("btwn", ["intradaymarketcap", CAP_MIN, CAP_MAX]),
    ])
    try:
        res = yf.screen(q, count=UNIVERSE_COUNT, sortField="percentchange", sortAsc=False)
    except Exception as e:
        print(f"[UNIVERSO] Error consultando screener: {e}")
        return []
    tickers = [qd.get("symbol") for qd in res.get("quotes", []) if qd.get("symbol")]
    print(f"[UNIVERSO] {len(tickers)} tickers micro/small cap activos hoy")
    return tickers


def find_historical_gainers(tickers: list[str], days: list) -> dict[str, list[dict]]:
    """{ticker: [{'date':..., 'pct_change':..., 'pdh':...}, ...]} para cada
    día en que el ticker subió > GAIN_MIN_PCT% (pdh = high del día previo,
    ya disponible en el mismo batch diario)."""
    start = (days[0] - timedelta(days=7)).isoformat()  # margen para tener el close/high previo
    end = (days[-1] + timedelta(days=1)).isoformat()
    gainers: dict[str, list[dict]] = {}
    day_strs = {d.isoformat() for d in days}

    CHUNK = 50
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            df = yf.download(chunk, start=start, end=end, interval="1d",
                              group_by="ticker", progress=False, auto_adjust=False, threads=True)
        except Exception as e:
            print(f"[DAILY] Error descargando chunk {i}-{i+len(chunk)}: {e}")
            continue
        for t in chunk:
            try:
                sub = df[t] if len(chunk) > 1 else df
            except Exception:
                continue
            sub = sub.dropna(how="all")
            if sub.empty or len(sub) < 2:
                continue
            for idx in range(1, len(sub)):
                date_str = sub.index[idx].strftime("%Y-%m-%d")
                if date_str not in day_strs:
                    continue
                close, prev_close = sub["Close"].iloc[idx], sub["Close"].iloc[idx - 1]
                if not prev_close:
                    continue
                pct = (close - prev_close) / prev_close * 100
                if pct > GAIN_MIN_PCT:
                    gainers.setdefault(t, []).append({
                        "date": date_str, "pct_change": round(float(pct), 1),
                        "pdh": round(float(sub["High"].iloc[idx - 1]), 4),
                    })
        print(f"[DAILY] Procesado chunk {i}-{i+len(chunk)}/{len(tickers)}")
        time.sleep(0.5)

    total = sum(len(v) for v in gainers.values())
    print(f"[GAINERS HISTÓRICOS] {total} día(s)-gainer encontrados en {len(gainers)} ticker(s)")
    return gainers


def simulate_breakout(ticker: str, date_str: str, pdh: float | None) -> dict | None:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    try:
        df = yf.Ticker(ticker).history(
            start=day.strftime("%Y-%m-%d"),
            end=(day + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="5m",
        )
    except Exception as e:
        print(f"[BAR] Error descargando 5m {ticker} {date_str}: {e}")
        return None
    if df.empty:
        return None
    df.index = df.index.tz_convert(ET)

    orb_window = df.between_time(MARKET_OPEN, ORB_END, inclusive="left")
    if orb_window.empty:
        return None
    orb15 = float(orb_window["High"].max())

    post_orb = df[df.index.strftime("%H:%M") >= ORB_END]
    breakout = None
    for ts, row in post_orb.iterrows():
        if row["Close"] > orb15:
            breakout = (ts, row)
            break

    base = {"ticker": ticker, "date": date_str, "orb15": round(orb15, 4), "pdh": pdh}
    if breakout is None:
        return {**base, "breakout": False}

    entry_ts, entry_row = breakout
    entry_price = float(entry_row["Close"])
    pdh_confirmed = bool(pdh and entry_price > pdh)

    checkpoints = {}
    for key, mins in CHECKPOINT_MINUTES.items():
        target = entry_ts + timedelta(minutes=mins)
        future = df[df.index >= target]
        checkpoints[key] = (
            round((float(future.iloc[0]["Close"]) - entry_price) / entry_price * 100, 2)
            if not future.empty else None
        )
    last_close = float(df.iloc[-1]["Close"])
    checkpoints["close"] = round((last_close - entry_price) / entry_price * 100, 2)

    return {
        **base, "breakout": True, "pdh_confirmed": pdh_confirmed,
        "entry_time": entry_ts.strftime("%H:%M"), "entry_price": round(entry_price, 4),
        "checkpoints": checkpoints,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=MAX_WEEKS_5M,
                         help=f"Semanas hacia atrás a cubrir (máx {MAX_WEEKS_5M}, límite de yfinance para velas de 5m)")
    args = parser.parse_args()
    weeks = min(args.weeks, MAX_WEEKS_5M)

    days = historical_range(weeks)
    print(f"[MAIN] Ventana: {days[0].isoformat()} a {days[-1].isoformat()} ({weeks} semanas, {len(days)} días laborables)")

    universe = get_broad_universe()
    if not universe:
        print("Sin universo, abortando")
        return

    gainers = find_historical_gainers(universe, days)

    results = []
    for ticker, entries in gainers.items():
        for e in entries:
            r = simulate_breakout(ticker, e["date"], e.get("pdh"))
            if r:
                r["pct_change_day"] = e["pct_change"]
                results.append(r)
            time.sleep(0.3)

    OUT_FILE.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    breakouts = [r for r in results if r.get("breakout")]
    print()
    print(f"[RESUMEN] {len(results)} días-gainer evaluados, {len(breakouts)} con breakout ORB15 confirmado")
    if breakouts:
        for key in list(CHECKPOINT_MINUTES.keys()) + ["close"]:
            vals = [r["checkpoints"][key] for r in breakouts if r["checkpoints"].get(key) is not None]
            if vals:
                avg = sum(vals) / len(vals)
                win = 100 * sum(1 for v in vals if v > 0) / len(vals)
                print(f"  {key}: retorno medio {avg:+.1f}% · win-rate {win:.0f}% (n={len(vals)})")

    print()
    print("Detalle por señal:")
    for r in sorted(breakouts, key=lambda x: x["date"]):
        cps = " · ".join(
            f"{k}:{v:+.1f}%" if v is not None else f"{k}:N/A" for k, v in r["checkpoints"].items()
        )
        pdh_tag = "PDH✓" if r.get("pdh_confirmed") else ("PDH✗" if r.get("pdh") else "PDH?")
        print(f"  {r['date']} {r['ticker']:6} entrada {r['entry_time']} ${r['entry_price']} {pdh_tag} | {cps}")

    print()
    print(f"[MAIN] Resultado completo guardado en {OUT_FILE}")


if __name__ == "__main__":
    main()
