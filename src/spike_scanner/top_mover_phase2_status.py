from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact

from spike_scanner.config import Settings
from spike_scanner.top_mover_learning import (
    TopMoverLearningConfig,
    TopMoverLearningStore,
)


MIN_USABLE = 1000
MIN_TRADING_DAYS = 20


def _volume(value: str) -> float:
    try:
        payload = json.loads(value or "{}")
        return float(payload.get("screener_volume"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return float("nan")


def main() -> None:
    settings = Settings.from_env(".env")
    config = TopMoverLearningConfig.from_env(settings)
    store = TopMoverLearningStore(config.database_path)

    with store.connect() as con:
        df = pd.read_sql_query(
            """
            SELECT
                source_run_id,
                signal_timestamp,
                due_at,
                outcome,
                features_json
            FROM top_mover_cases
            WHERE cohort = 'TOP_MOVER'
              AND status = 'finalized'
              AND data_quality = 'usable'
            ORDER BY signal_timestamp, source_run_id
            """,
            con,
        )

    print()
    print("TOP-MOVER PHASE-2 STATUS")
    print("=" * 64)
    print("Datenbank:", store.path.resolve())

    if df.empty:
        print("Noch keine usable/finalized TOP_MOVER-Faelle.")
        return

    df["signal_timestamp"] = pd.to_datetime(
        df["signal_timestamp"], utc=True
    )
    df["due_at"] = pd.to_datetime(
        df["due_at"], utc=True
    )

    usable = len(df)
    trading_days = df["signal_timestamp"].dt.date.nunique()

    enough_cases = usable >= MIN_USABLE
    enough_days = trading_days >= MIN_TRADING_DAYS
    ready = enough_cases and enough_days

    print()
    print("DATENBASIS")
    print("-" * 64)
    print(f"Usable/finalized TOP_MOVER : {usable}")
    print(f"Handelstage                : {trading_days}")
    print(f"Ziel Faelle                : >= {MIN_USABLE}")
    print(f"Ziel Handelstage           : >= {MIN_TRADING_DAYS}")

    missing_cases = max(0, MIN_USABLE - usable)
    missing_days = max(0, MIN_TRADING_DAYS - trading_days)

    print(f"Noch fehlende Faelle       : {missing_cases}")
    print(f"Noch fehlende Handelstage  : {missing_days}")

    print()
    if ready:
        print("PHASE-2 RETEST: FREIGEGEBEN")
        print("Die Analyse darf erneut mit Purged Walk-Forward laufen.")
    else:
        print("PHASE-2 RETEST: NOCH NICHT FREIGEGEBEN")
        print("Weiter Daten sammeln.")

    binary = df[
        df["outcome"].isin(
            ["target_before_stop", "stop_before_target"]
        )
    ].copy()

    if len(binary) < 20:
        print()
        print("Q75: Noch zu wenige eindeutige Faelle.")
        return

    binary = binary.reset_index(drop=True)
    binary["volume"] = binary["features_json"].map(_volume)
    binary["target"] = (
        binary["outcome"] == "target_before_stop"
    ).astype(int)

    binary = binary.dropna(subset=["volume"])

    runs = (
        binary[["source_run_id", "signal_timestamp"]]
        .drop_duplicates("source_run_id")
        .sort_values("signal_timestamp")
        .reset_index(drop=True)
    )

    if len(runs) < 10:
        print()
        print("Q75: Noch zu wenige unabhaengige Scanner-Runs.")
        return

    test_runs = runs.iloc[len(runs) // 2 :]
    blocks = np.array_split(
        test_runs["source_run_id"].to_numpy(),
        5,
    )

    oof = []

    for fold, block_runs in enumerate(blocks, 1):
        if len(block_runs) == 0:
            continue

        test = binary[
            binary["source_run_id"].isin(block_runs)
        ].copy()

        if test.empty:
            continue

        test_start = test["signal_timestamp"].min()

        train = binary[
            (binary["signal_timestamp"] < test_start)
            & (binary["due_at"] <= test_start)
        ].copy()

        if train.empty:
            continue

        q75 = train["volume"].quantile(0.75)

        test["fold"] = fold
        test["q75_threshold"] = q75
        test["high_volume"] = test["volume"] >= q75

        oof.append(test)

    print()
    print("Q75 BEOBACHTUNGSKANDIDAT")
    print("-" * 64)
    print("Regel: screener_volume >= historisches Q75")
    print("Produktive Wirkung: NEIN")

    if not oof:
        print("Noch keine ausreichende OOS-Auswertung moeglich.")
        return

    oof = pd.concat(oof, ignore_index=True)

    high = oof[oof["high_volume"]]
    low = oof[~oof["high_volume"]]

    high_target = int(high["target"].sum())
    high_stop = len(high) - high_target

    low_target = int(low["target"].sum())
    low_stop = len(low) - low_target

    high_rate = (
        high_target / len(high)
        if len(high)
        else float("nan")
    )

    low_rate = (
        low_target / len(low)
        if len(low)
        else float("nan")
    )

    print(f"OOS Testfaelle             : {len(oof)}")
    print(f"Q75 High-Volume Faelle     : {len(high)}")
    print(f"Q75 Target vor Stop        : {high_target}")
    print(f"Q75 Stop vor Target        : {high_stop}")

    if not math.isnan(high_rate):
        print(f"Q75 Target-Rate            : {high_rate:.3f}")

    print(f"Unter-Q75 Faelle           : {len(low)}")
    print(f"Unter-Q75 Target           : {low_target}")
    print(f"Unter-Q75 Stop             : {low_stop}")

    if not math.isnan(low_rate):
        print(f"Unter-Q75 Target-Rate      : {low_rate:.3f}")

    if (
        len(high)
        and len(low)
        and high_target + high_stop > 0
        and low_target + low_stop > 0
    ):
        odds, p_value = fisher_exact(
            [
                [high_target, high_stop],
                [low_target, low_stop],
            ],
            alternative="greater",
        )

        diff = high_rate - low_rate
        lift = (
            high_rate / low_rate
            if low_rate > 0
            else float("inf")
        )

        print(f"Absolute Differenz         : {diff:.3f}")
        print(f"Lift gegen Rest            : {lift:.3f}")
        print(f"Odds Ratio                 : {odds:.3f}")
        print(f"Fisher p-Wert              : {p_value:.4f}")

    print()
    print("STATUS")
    print("-" * 64)

    if ready:
        print("Datenschwelle erreicht: Phase-2-Retest vorbereiten.")
    else:
        print("Weiter beobachten. Noch keine produktive Phase-2-Regel.")


if __name__ == "__main__":
    main()
