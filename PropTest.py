#!/usr/bin/env python3
"""
Backtest of the "fixed-size, no-edge" prop firm strategy (Youngblood, 2026)
on NQ 1-minute CSV data, run through a typical prop firm evaluation -> funded account lifecycle.

STRATEGY (per the paper)
  * One trade per day, entry at the OPEN of the 10:00 ET bar
  * Direction random (50/50), 100 ticks (25 pts) TP and SL, 8 MNQ-equivalent contracts
    -> +/- $400 gross per trade
  * Time exit at 16:00 ET if neither barrier is hit
  * Days with TP and SL inside the same 1-min bar are excluded (order unknowable)
  * Days with no 10:00 bar or an early close are excluded

PROP FIRM RULES (configurable below)
  Evaluation: $50k balance, $3k target, $2k EOD trailing drawdown (locks at start balance)
  Funded    : starts at $50k, $2k EOD trailing drawdown; once the balance passes $52,100 it is
              static at $50,100. Payout when the balance is >= $54k (min 5 days since the last one),
              request = min(50% of profit, $2k cap), 90% split
  Billing: one-time fee per evaluation; a failed evaluation (or blown funded account) means buying a new one.
           No resets (a reset usually costs more than a new account). Optional monthly
           rebill of a still-running evaluation with --rebill (off by default; few firms use it).

CSV FORMAT
  Row 1: "Time Series,NQH26,,,,,"   (skipped automatically)
  Row 2: Date,Symbol,Open,High,Low,Close,Volume
  Row 3: 1/30/2026 3:55 PM,NQH26,"25.640,75","25.648,50","25.634,25","25.639,25",739
  Timestamps are Chicago time -> +1 hour is added to get New York time.
  If several contracts (NQH26, NQM26, ...) are in the file, the highest-volume
  contract of each day is used.

USAGE
  python PropTest.py NQ_1min.csv
  python PropTest.py NQ_1min.csv --paths 10000 --seed 42 --start 2021-08-09
  python PropTest.py NQ_1min.csv --direction long      # single deterministic path

  Optional speed-up (~50x):  pip install numba
"""
import argparse
import sys
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
CSV_SHIFT_HOURS   = 1        # CSV is Chicago time -> +1h = New York time
ENTRY_MINUTE      = 10 * 60  # 10:00 ET
EXIT_MINUTE       = 16 * 60  # 16:00 ET (time exit)
MIN_LAST_BAR      = 15 * 60 + 55   # need a bar at/after 15:55 ET -> "full session"

TP_POINTS         = 25.0     # 100 ticks * 0.25
SL_POINTS         = 25.0
POINT_VALUE       = 2.0      # MNQ = $2/pt (use 20.0 for full-size NQ)
CONTRACTS         = 8        # 25 pts * $2 * 8 = $400
COMMISSION_RT     = 1.22     # $ round-turn PER CONTRACT (paper: $1.22 MNQ)

# Evaluation
EVAL_BALANCE      = 50_000.0
EVAL_TARGET       = 3_000.0
EVAL_DD           = 2_000.0  # EOD trailing, locks at starting balance
EVAL_CONSIST      = 0.50     # best day <= 50% of total profit
EVAL_FEE          = 90.0     # $ per evaluation (paper used $85) - set to your real price
MONTHLY_REBILL    = False    # True: an evaluation still running after BILLING_DAYS is charged again
BILLING_DAYS      = 30

# Funded account
FUNDED_TARGET     = 4_000.0  # payout allowed once the balance is this far above start (54k)
FUNDED_DD         = 2_000.0  # EOD trailing ...
FUNDED_LOCK       = 100.0    # ... until the peak reaches start + DD + LOCK (52,100), then static at start + LOCK (50,100)
FUNDED_CAP        = 2_000.0  # max single payout
FUNDED_FRACTION   = 0.50     # max payout = 50% of profit
FUNDED_SPLIT      = 0.90     # trader keeps 90%
FUNDED_CONSIST    = 0.0      # best day <= X of cycle profit; 0 = no consistency rule
FUNDED_MIN_DAYS   = 5        # min trading days per payout cycle
FUNDED_MIN_PAYOUT = 125.0
FUNDED_FEE        = 0.0      # one-off activation fee when moving evaluation -> funded

# ----------------------------------------------------------------------------
# 1) DATA -> one row per eligible trading day
# ----------------------------------------------------------------------------
def _is_european(sample: str) -> bool:
    """25.640,75 / 3575,75 -> True ; 25640.75 / 25,640.75 -> False"""
    return sample.rfind(",") > sample.rfind(".")


def _to_float(s: pd.Series, european: bool) -> pd.Series:
    s = s.astype(str).str.strip()
    if european:
        s = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    else:
        s = s.str.replace(",", "", regex=False)
    return pd.to_numeric(s, errors="coerce")


def load_csv(path: str) -> pd.DataFrame:
    with open(path, "r", encoding="utf-8-sig") as f:
        first = f.readline().strip()
        header = f.readline().strip() if first.lower().startswith("time series") else first

    skip = 1 if first.lower().startswith("time series") else 0
    sep = ";" if header.count(";") > header.count(",") else ","

    df = pd.read_csv(
        path,
        sep=sep,
        skiprows=skip,
        dtype=str,
        encoding="utf-8-sig",
    )

    # Normalize column names
    df.columns = (
        df.columns.astype(str)
        .str.replace("\ufeff", "", regex=False)
        .str.strip()
    )

    # Fail early if the CSV format is unexpected
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            f"Missing columns: {missing}. "
            f"Actual columns: {df.columns.tolist()}"
        )

    # Remove repeated header rows
    df = df[
        df["Date"].astype(str).str.strip().str.lower() != "date"
    ].copy()

    # Parse timestamps
    ts = pd.to_datetime(
        df["Date"].astype(str).str.strip(),
        format="%m/%d/%Y %I:%M %p",
        errors="coerce",
    )

    missing_ts = ts.isna()
    if missing_ts.any():
        ts.loc[missing_ts] = pd.to_datetime(
            df.loc[missing_ts, "Date"].astype(str).str.strip(),
            errors="coerce",
        )

    df["ts"] = ts + pd.Timedelta(hours=CSV_SHIFT_HOURS)

    # Number format is detected once from the prices: European 3.575,75 or plain 3575.75 / 3,575.75
    sample = df["Open"].dropna().astype(str).str.strip()
    european = _is_european(sample.iloc[0]) if len(sample) else False
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = _to_float(df[c], european)

    df = df.dropna(subset=["ts", "Open", "High", "Low", "Close"])

    df["date"] = df["ts"].dt.normalize()
    df["minute"] = df["ts"].dt.hour * 60 + df["ts"].dt.minute

    print("Loaded columns:", df.columns.tolist())
    print(df[["Date", "Open", "High", "Low", "Close", "Volume"]].head(3))

    return df.sort_values("ts").reset_index(drop=True)




def build_daily_table(df: pd.DataFrame) -> pd.DataFrame:
    # keep only the most-liquid contract per day (handles rollovers)
    if "Symbol" in df.columns and df["Symbol"].nunique() > 1:
        vol = df.groupby(["date", "Symbol"])["Volume"].sum().reset_index()
        best = vol.loc[vol.groupby("date")["Volume"].idxmax(), ["date", "Symbol"]]
        df = df.merge(best, on=["date", "Symbol"])

    tp_usd = TP_POINTS * POINT_VALUE * CONTRACTS
    sl_usd = SL_POINTS * POINT_VALUE * CONTRACTS
    rows, stats = [], dict(no_entry=0, early=0, ambiguous=0)

    for date, g in df.groupby("date"):
        g = g[(g["minute"] >= ENTRY_MINUTE) & (g["minute"] < EXIT_MINUTE)]
        if g.empty or g["minute"].iloc[0] != ENTRY_MINUTE:
            stats["no_entry"] += 1
            continue
        if g["minute"].iloc[-1] < MIN_LAST_BAR:
            stats["early"] += 1
            continue

        entry = g["Open"].iloc[0]
        hi, lo = g["High"].to_numpy(), g["Low"].to_numpy()
        up_hit, dn_hit = hi >= entry + TP_POINTS, lo <= entry - SL_POINTS
        first_up = int(up_hit.argmax()) if up_hit.any() else 10**9
        first_dn = int(dn_hit.argmax()) if dn_hit.any() else 10**9

        if first_up == first_dn and first_up < 10**9:
            stats["ambiguous"] += 1
            continue
        if first_up < first_dn:
            outcome, long_gross = 1, tp_usd              # long wins / short loses
        elif first_dn < first_up:
            outcome, long_gross = -1, -sl_usd            # long loses / short wins
        else:                                            # time exit
            outcome = 0
            long_gross = (g["Close"].iloc[-1] - entry) * POINT_VALUE * CONTRACTS
        rows.append((date, entry, outcome, long_gross))

    daily = pd.DataFrame(rows, columns=["date", "entry", "outcome", "long_gross"])
    print(f"Eligible days: {len(daily)}  | skipped: {stats['no_entry']} no 10:00 bar, "
          f"{stats['early']} early close, {stats['ambiguous']} ambiguous TP/SL same bar")
    return daily

# ----------------------------------------------------------------------------
# 2) PROP FIRM SIMULATION (numba-compatible: only scalars and numpy arrays)
# ----------------------------------------------------------------------------
def _simulate(day_ord, long_gross, dirs, comm,
              fee, c_bal, c_target, c_dd, c_cons,
              x_target, x_dd, x_lock, x_cap, x_frac, x_split, x_cons, x_mindays, x_minpay,
              x_fee, rebill, billing):
    if c_cons <= 0:                    # 0 = no consistency rule
        c_cons = np.inf
    if x_cons <= 0:
        x_cons = np.inf
    n_paths, n_days = dirs.shape
    out = np.zeros((n_paths, 7))
    for p in range(n_paths):
        phase = 0                      # 0 = evaluation, 1 = funded
        costs = fee
        payouts = 0.0
        n_comb = 1
        n_rebill = 0
        n_pass = 0
        n_fail = 0
        n_pay = 0
        next_bill = day_ord[0] + billing
        bal = c_bal
        peak = c_bal
        mll = c_bal - c_dd
        best = 0.0
        cyc_profit = 0.0
        cyc_days = 0

        for i in range(n_days):
            d = day_ord[i]
            if phase == 0 and rebill:
                while d >= next_bill:                  # optional monthly rebill of a running evaluation
                    costs += fee
                    n_rebill += 1
                    next_bill += billing

            pnl = dirs[p, i] * long_gross[i] - comm
            bal += pnl

            if phase == 0:                             # ---------------- EVALUATION
                if pnl > best:
                    best = pnl
                if bal <= mll:                         # EOD drawdown breach -> buy a new evaluation
                    costs += fee
                    n_comb += 1
                    next_bill = d + billing
                    bal = c_bal
                    peak = c_bal
                    mll = c_bal - c_dd
                    best = 0.0
                    continue
                if bal > peak:
                    peak = bal
                    mll = min(peak - c_dd, c_bal)      # trails, locks at start balance
                profit = bal - c_bal
                if profit >= c_target and best <= c_cons * profit:
                    n_pass += 1                        # -> funded
                    costs += x_fee
                    phase = 1
                    bal = 0.0
                    peak = 0.0
                    mll = -x_dd
                    cyc_profit = 0.0
                    cyc_days = 0
                    best = 0.0
            else:                                      # ---------------- FUNDED
                if bal <= mll:                         # funded blown -> buy new evaluation
                    n_fail += 1
                    phase = 0
                    costs += fee
                    n_comb += 1
                    next_bill = d + billing
                    bal = c_bal
                    peak = c_bal
                    mll = c_bal - c_dd
                    best = 0.0
                    continue
                if bal > peak:
                    peak = bal
                    mll = min(peak - x_dd, x_lock)     # trails, then locks at start + x_lock
                cyc_profit += pnl
                cyc_days += 1
                if pnl > best:
                    best = pnl
                if (bal >= x_target and cyc_days >= x_mindays  # balance-based threshold (54k)
                        and best <= x_cons * cyc_profit):
                    req = min(x_frac * bal, x_cap)
                    if req >= x_minpay:
                        payouts += req * x_split
                        bal -= req
                        n_pay += 1
                        cyc_profit = 0.0
                        cyc_days = 0
                        best = 0.0

        out[p, 0] = payouts
        out[p, 1] = costs
        out[p, 2] = n_comb
        out[p, 3] = n_rebill
        out[p, 4] = n_pass
        out[p, 5] = n_fail
        out[p, 6] = n_pay
    return out


try:
    from numba import njit
    _simulate = njit(cache=True)(_simulate)
    HAVE_NUMBA = True
except ImportError:
    HAVE_NUMBA = False

# ----------------------------------------------------------------------------
# 3) RUNNER
# ----------------------------------------------------------------------------
def run(daily: pd.DataFrame, n_paths: int, seed: int, direction: str,
        rebill: bool = MONTHLY_REBILL, chunk: int = 1000):
    day_ord = daily["date"].map(pd.Timestamp.toordinal).to_numpy(dtype=np.int64)
    long_gross = daily["long_gross"].to_numpy(dtype=np.float64)
    outcome = daily["outcome"].to_numpy()
    n_days = len(daily)
    comm = COMMISSION_RT * CONTRACTS

    if direction != "random":
        n_paths = 1
    rng = np.random.default_rng(seed)

    res, raw = [], []
    for start in range(0, n_paths, chunk):
        m = min(chunk, n_paths - start)
        if direction == "long":
            dirs = np.ones((1, n_days), dtype=np.int8)
        elif direction == "short":
            dirs = -np.ones((1, n_days), dtype=np.int8)
        else:
            dirs = rng.choice(np.array([-1, 1], dtype=np.int8), size=(m, n_days))

        res.append(_simulate(day_ord, long_gross, dirs, comm,
                             EVAL_FEE, EVAL_BALANCE, EVAL_TARGET, EVAL_DD, EVAL_CONSIST,
                             FUNDED_TARGET, FUNDED_DD, FUNDED_LOCK, FUNDED_CAP, FUNDED_FRACTION, FUNDED_SPLIT,
                             FUNDED_CONSIST,
                             FUNDED_MIN_DAYS, FUNDED_MIN_PAYOUT, FUNDED_FEE, rebill, BILLING_DAYS))

        # raw (no prop firm) stats
        gross = dirs.astype(np.float64) @ long_gross
        barrier = outcome != 0
        wins = ((dirs == outcome) & barrier).sum(axis=1)
        raw.append(np.column_stack([gross, wins, np.full(len(gross), barrier.sum())]))

    r = np.vstack(res)
    w = np.vstack(raw)
    df = pd.DataFrame(r, columns=["payouts", "account_costs", "evals_bought", "rebills",
                                  "eval_passes", "funded_failures", "payouts_n"])
    df["net"] = df["payouts"] - df["account_costs"]
    df["raw_gross"] = w[:, 0]
    df["raw_net"] = w[:, 0] - comm * n_days
    df["barrier_wins"] = w[:, 1]
    df["barrier_trades"] = w[:, 2]
    return df


def report(df: pd.DataFrame, n_days: int):
    n = len(df)
    sep = "-" * 60
    print(f"\n{sep}\nRAW STRATEGY (no prop firm)  |  {n} path(s), {n_days} trades each\n{sep}")
    print(f"Barrier win rate        : {df.barrier_wins.sum() / df.barrier_trades.sum():.2%}")
    print(f"Mean gross P&L / path   : ${df.raw_gross.mean():,.2f}  (median ${df.raw_gross.median():,.2f})")
    print(f"Mean net P&L / path     : ${df.raw_net.mean():,.2f}  (after commissions)")

    print(f"\n{sep}\nPROP FIRM RESULT (single account sequence)\n{sep}")
    print(f"Mean payouts            : ${df.payouts.mean():,.2f}")
    print(f"Mean account costs      : ${df.account_costs.mean():,.2f}")
    print(f"Mean net economic P&L   : ${df.net.mean():,.2f}  (median ${df.net.median():,.2f})")
    ratio = df.payouts.mean() / df.account_costs.mean() if df.account_costs.mean() else float("nan")
    print(f"Payout / cost ratio     : {ratio:.3f}")
    print(f"% paths profitable      : {(df.net > 0).mean():.2%}")
    if n > 1:
        q = df.net.quantile([.05, .25, .75, .95])
        print(f"Std dev of net          : ${df.net.std():,.2f}")
        print(f"Net P&L 5/25/75/95 pct  : ${q[.05]:,.0f} / ${q[.25]:,.0f} / ${q[.75]:,.0f} / ${q[.95]:,.0f}")
    print(f"\nPer path averages: evaluations bought {df.evals_bought.mean():.2f}, "
          f"rebills {df.rebills.mean():.2f},\n"
          f"                   evaluation passes {df.eval_passes.mean():.2f}, "
          f"funded failures {df.funded_failures.mean():.2f}, payouts {df.payouts_n.mean():.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--paths", type=int, default=10_000, help="random-direction paths (default 10000)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--direction", choices=["random", "long", "short"], default="random")
    ap.add_argument("--start", help="first date to include, e.g. 2021-08-09")
    ap.add_argument("--end", help="last date to include")
    ap.add_argument("--out", default="prop_backtest", help="prefix for output CSVs")
    ap.add_argument("--rebill", action="store_true", default=MONTHLY_REBILL,
                    help="charge the evaluation fee again every 30 days while it runs")
    a = ap.parse_args()

    print(f"numba: {'yes' if HAVE_NUMBA else 'no (pure Python - install numba for ~50x speed)'}")
    df = load_csv(a.csv)
    print(f"Loaded {len(df):,} bars, {df.ts.min()} -> {df.ts.max()} (New York time)")
    daily = build_daily_table(df)
    if a.start:
        daily = daily[daily.date >= pd.Timestamp(a.start)]
    if a.end:
        daily = daily[daily.date <= pd.Timestamp(a.end)]
    daily = daily.reset_index(drop=True)
    if len(daily) < 20:
        sys.exit("Too few eligible days after filtering.")

    res = run(daily, a.paths, a.seed, a.direction, a.rebill)
    report(res, len(daily))
    daily.to_csv(f"{a.out}_daily.csv", index=False)
    res.to_csv(f"{a.out}_paths.csv", index=False)
    print(f"\nSaved {a.out}_daily.csv and {a.out}_paths.csv")


if __name__ == "__main__":
    main()