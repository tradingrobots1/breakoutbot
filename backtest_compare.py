#!/usr/bin/env python3
"""
BREAKOUTBOT — Comparador de variantes de entrada (puntual, exploratorio)

Reutiliza la lista de días-gainer ya identificados por backtest_breakout.py
(backtest_results.json) y, para cada uno, descarga sus velas de 5m UNA vez
y calcula el resultado de varias reglas de entrada distintas sobre los
mismos datos — para comparar cuál habría rendido mejor:

  - open        : comprar al Open de la primera vela (09:30 ET), sin
                   esperar nada — el "no-mecanismo", línea base.
  - orb_end     : comprar al cierre de la ventana ORB (~09:45 ET), tanto
                   si luego hay breakout como si no.
  - breakout    : la regla de producción — comprar en el cierre de la
                   primera vela de 5m tras el ORB que supera el ORB15.
  - breakout_no_pdh : subconjunto de "breakout" donde el precio de entrada
                   NO había roto ya el PDH (el hallazgo del backtest
                   anterior: rendía mejor que cuando sí lo rompía).

Requiere haber ejecutado antes backtest_breakout.py (usa su
backtest_results.json para la lista de tickers/días/ORB15/PDH).

Uso:
  python3 backtest_compare.py
"""
import json
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")

MARKET_OPEN = "09:30"
ORB_END = "09:45"
CHECKPOINT_MINUTES = {"m15": 15, "m30": 30, "h1": 60, "h2": 120}

RESULTS_FILE = Path(__file__).parent / "backtest_results.json"
OUT_FILE = Path(__file__).parent / "backtest_compare_results.json"


def load_targets() -> list[dict]:
    data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return [{
        "ticker": r["ticker"], "date": r["date"], "orb15": r.get("orb15"),
        "pdh": r.get("pdh"), "pdh_confirmed_prod": r.get("pdh_confirmed", False),
    } for r in data if r.get("orb15") is not None]


def _checkpoints_from(df, entry_ts, entry_price: float) -> dict:
    cps = {}
    for key, mins in CHECKPOINT_MINUTES.items():
        target = entry_ts + timedelta(minutes=mins)
        future = df[df.index >= target]
        cps[key] = (
            round((float(future.iloc[0]["Close"]) - entry_price) / entry_price * 100, 2)
            if not future.empty else None
        )
    last_close = float(df.iloc[-1]["Close"])
    cps["close"] = round((last_close - entry_price) / entry_price * 100, 2)
    return cps


def compute_variants(ticker: str, date_str: str, orb15: float) -> dict | None:
    day_start = date_str
    day_end = (
        __import__("datetime").datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")
    try:
        df = yf.Ticker(ticker).history(start=day_start, end=day_end, interval="5m")
    except Exception as e:
        print(f"[BAR] Error descargando 5m {ticker} {date_str}: {e}")
        return None
    if df.empty:
        return None
    df.index = df.index.tz_convert(ET)

    variants = {}

    # open: primera vela del día
    first_ts = df.index[0]
    open_price = float(df.iloc[0]["Open"])
    variants["open"] = _checkpoints_from(df, first_ts, open_price)

    # orb_end: cierre de la última vela de la ventana ORB
    orb_window = df.between_time(MARKET_OPEN, ORB_END, inclusive="left")
    if not orb_window.empty:
        orb_end_ts = orb_window.index[-1]
        orb_end_price = float(orb_window.iloc[-1]["Close"])
        variants["orb_end"] = _checkpoints_from(df, orb_end_ts, orb_end_price)

    # breakout: primera vela tras el ORB con Close > orb15 (regla de producción)
    post_orb = df[df.index.strftime("%H:%M") >= ORB_END]
    for ts, row in post_orb.iterrows():
        if row["Close"] > orb15:
            variants["breakout"] = _checkpoints_from(df, ts, float(row["Close"]))
            break

    return variants


def aggregate(rows: list[dict], key: str) -> tuple[float, float, int] | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    win = 100 * sum(1 for v in vals if v > 0) / len(vals)
    return avg, win, len(vals)


def main():
    targets = load_targets()
    print(f"[MAIN] {len(targets)} días-gainer a re-simular con distintas entradas")

    per_variant: dict[str, list[dict]] = {"open": [], "orb_end": [], "breakout": [], "breakout_no_pdh": []}

    for i, t in enumerate(targets):
        variants = compute_variants(t["ticker"], t["date"], t["orb15"])
        if variants is None:
            continue
        for name in ("open", "orb_end", "breakout"):
            if name in variants:
                per_variant[name].append(variants[name])
        if "breakout" in variants and not t["pdh_confirmed_prod"]:
            per_variant["breakout_no_pdh"].append(variants["breakout"])
        if (i + 1) % 20 == 0:
            print(f"[MAIN] {i+1}/{len(targets)} procesados")
        time.sleep(0.25)

    OUT_FILE.write_text(json.dumps(per_variant, indent=2, default=str), encoding="utf-8")

    print()
    print("Comparación de reglas de entrada — retorno medio / win-rate por checkpoint:")
    print()
    header = f"{'variante':18} | " + " | ".join(f"{k:>16}" for k in list(CHECKPOINT_MINUTES.keys()) + ["close"])
    print(header)
    print("-" * len(header))
    for name, rows in per_variant.items():
        if not rows:
            continue
        cells = []
        for key in list(CHECKPOINT_MINUTES.keys()) + ["close"]:
            s = aggregate(rows, key)
            cells.append(f"{s[0]:+5.1f}%/{s[1]:3.0f}%n{s[2]:<3}" if s else " " * 16)
        print(f"{name:18} | " + " | ".join(f"{c:>16}" for c in cells))

    print()
    print(f"[MAIN] Detalle completo guardado en {OUT_FILE}")


if __name__ == "__main__":
    main()
