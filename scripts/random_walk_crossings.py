"""
How often does MACD(12, 26, 9) cross its signal when there is nothing to find?

    python scripts/random_walk_crossings.py

Prices are simulated as random walks: independent daily returns with no drift
and 3% volatility, so any crossover is noise by construction. Crossings are
counted the way the summary panel counts them (macd_toolkit.resumen_tecnico):
valid histogram periods divided by the number of MACD/signal crossovers.
The average interval is the baseline behind PERIODOS_RUIDO, the threshold the
panel uses to flag a ticker whose crossovers are as frequent as noise.

The last part applies that threshold, and the previous one of 10, to the daily
run over 35 BMV tickers stored in examples/bmv-daily-2026-10-02.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import macd_toolkit as mt  # noqa: E402

SIMULATIONS = 40
STEPS = 3000
DAILY_VOL = 0.03
SEED = 0


def periods_per_crossing(close: pd.Series) -> float:
    df = mt.macd(close)
    events = mt.crossovers(df)
    crossings = int(events["evento"].isin(["cruce_alcista", "cruce_bajista"]).sum())
    return int(df["hist"].notna().sum()) / crossings


def random_walk_baseline(simulations: int = SIMULATIONS, steps: int = STEPS,
                         seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(simulations):
        returns = rng.normal(0.0, DAILY_VOL, steps)
        out.append(periods_per_crossing(pd.Series(100.0 * np.exp(np.cumsum(returns)))))
    return np.array(out)


def main() -> None:
    f = random_walk_baseline()
    print(f"Random walks: {SIMULATIONS} x {STEPS} steps, daily volatility {DAILY_VOL:.0%}, seed {SEED}")
    print(f"MACD(12, 26, 9) crosses its signal once every {f.mean():.1f} periods on average")
    print(f"  range {f.min():.1f} to {f.max():.1f}, median {np.median(f):.1f}")
    print(f"  on 1-minute candles that is about {1440 / f.mean():.0f} crossings a day")
    print(f"Panel threshold PERIODOS_RUIDO = {mt.PERIODOS_RUIDO}")

    summary = ROOT / "examples" / "bmv-daily-2026-10-02" / "resumen.csv"
    if summary.exists():
        rows = pd.read_csv(summary)
        # Valid histogram periods for a series of that length, as the toolkit counts them.
        valid = rows["periodos"].map(
            lambda n: int(mt.macd(pd.Series(np.linspace(1.0, 2.0, n)))["hist"].notna().sum()))
        freq = valid / rows["cruces_totales"]
        print(f"\nBMV daily run of 2 October 2026 ({len(rows)} tickers):")
        for threshold in (10, mt.PERIODOS_RUIDO):
            flagged = rows.loc[freq < threshold, "ticker"].tolist()
            print(f"  fewer than {threshold} periods between crossings: {len(flagged)} tickers")
        print(f"  median interval {freq.median():.1f} periods, "
              f"from {freq.min():.1f} to {freq.max():.1f}")


if __name__ == "__main__":
    main()
