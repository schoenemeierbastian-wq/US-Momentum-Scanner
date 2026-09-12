from __future__ import annotations

import json

import pandas as pd

from spike_scanner.config import Settings
from spike_scanner.german import feature_label, format_feature, probability_text
from spike_scanner.storage import Storage
from spike_scanner.scoring import (
    MIN_TOP_SIGNAL_SCORE, combined_score, ranking_score, market_phase_at,
    purchase_assessment, signal_age_minutes,
)


def run() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise RuntimeError("Dashboard benötigt: pip install streamlit") from exc

    settings = Settings.from_env()
    storage = Storage(settings.database_path)
    st.set_page_config(page_title="US Momentum Scanner", layout="wide")

    st.markdown(
        """
        <style>
        .stApp { background-color: #0f172a; color: #f8fafc; }
        [data-testid="stHeader"] { background-color: rgba(15, 23, 42, 0.92); }
        [data-testid="stMetric"] { background: #182235; border: 1px solid #334155; padding: 12px; border-radius: 10px; }
        div[data-testid="stDataFrame"] { border: 1px solid #334155; border-radius: 10px; }
        h1, h2, h3 { color: #f8fafc; }
        .stCaption, small { color: #94a3b8 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("US Momentum Scanner")
    st.caption(
        "Signal-Score = Momentum-/Volumenstärke. Risiko-Score = Ausführungsrisiko. "
        "Gesamt-Score = risikoadjustierter Signal-Score. "
        f"Top-Kandidaten brauchen mindestens Signal-Score {MIN_TOP_SIGNAL_SCORE:.0f}. "
        "Ranking-Score = 50 % Gesamt + 30 % Modell-P + 20 % Signal. "
        "Kauf-Score (Forschung) = Gesamt + Ziel-P + Signal + Frische + Ausfuehrung; keine Eintrittswahrscheinlichkeit."
    )

    frame = storage.latest_frame()
    if frame.empty:
        st.info("Noch keine Scans vorhanden. Zuerst einen Scan in der Windows-Oberfläche oder per Kommando ausführen.")
        return

    recommendation_map = storage.recommendations_for(frame["symbol"].astype(str).tolist())

    def assessment_for(row):
        recommendation = recommendation_map.get(str(row["symbol"]).upper(), {})
        first_seen = recommendation.get("first_seen_at") or row.get("timestamp")
        try:
            probability = float(row.get("model_probability"))
            if probability != probability:
                probability = None
        except (TypeError, ValueError):
            probability = None
        try:
            features = json.loads(row.get("features_json") or "{}")
        except Exception:
            features = {}
        return purchase_assessment(
            row["signal_score"], row["risk_score"], probability, features,
            age_minutes=signal_age_minutes(first_seen),
            phase=market_phase_at(first_seen),
        )

    selected = frame[frame["selected_top"] == 1].copy()
    selected["Gründe"] = selected["reasons_json"].map(lambda value: " | ".join(json.loads(value)))
    selected["Gesamt-Score"] = selected.apply(
        lambda row: combined_score(row["signal_score"], row["risk_score"]), axis=1
    )
    selected["Ranking-Score"] = selected.apply(
        lambda row: ranking_score(row["signal_score"], row["risk_score"], row["model_probability"]), axis=1
    )
    selected["Modellwahrscheinlichkeit"] = selected["model_probability"].map(probability_text)
    selected_assessments = selected.apply(assessment_for, axis=1)
    selected["Kauf-Score"] = [a.score for a in selected_assessments]
    selected["Kauf-Qualität"] = [a.quality for a in selected_assessments]
    selected = selected.rename(
        columns={
            "rank": "Rang",
            "symbol": "Kürzel",
            "price": "Kurs (USD)",
            "signal_score": "Signal-Score",
            "risk_score": "Risiko-Score",
        }
    )
    columns = ["Rang", "Kürzel", "Kurs (USD)", "Signal-Score", "Risiko-Score", "Gesamt-Score", "Ranking-Score", "Kauf-Score", "Kauf-Qualität", "Modellwahrscheinlichkeit", "Gründe"]
    st.subheader("Aktuelle Top-Kandidaten")
    st.dataframe(selected[columns], use_container_width=True, hide_index=True)

    universe = frame.rename(
        columns={
            "rank": "Rang",
            "symbol": "Kürzel",
            "price": "Kurs (USD)",
            "signal_score": "Signal-Score",
            "risk_score": "Risiko-Score",
            "selected_top": "Top-Auswahl",
        }
    ).copy()
    universe["Gesamt-Score"] = universe.apply(
        lambda row: combined_score(row["Signal-Score"], row["Risiko-Score"]), axis=1
    )
    universe["Ranking-Score"] = universe.apply(
        lambda row: ranking_score(row["Signal-Score"], row["Risiko-Score"], row.get("model_probability")), axis=1
    )
    # assessment_for erwartet die urspruenglichen Spaltennamen; deshalb ueber den Index aus frame.
    universe_assessments = [assessment_for(frame.loc[idx]) for idx in universe.index]
    universe["Kauf-Score"] = [a.score for a in universe_assessments]
    universe["Kauf-Qualität"] = [a.quality for a in universe_assessments]
    universe["Top-Auswahl"] = universe["Top-Auswahl"].map({1: "Ja", 0: "Nein"})
    st.subheader("Gesamtes untersuchtes Universum")
    st.dataframe(
        universe[["Rang", "Kürzel", "Kurs (USD)", "Signal-Score", "Risiko-Score", "Gesamt-Score", "Ranking-Score", "Kauf-Score", "Kauf-Qualität", "Top-Auswahl"]],
        use_container_width=True,
        hide_index=True,
    )

    if selected.empty:
        st.info("Im letzten Scan wurden keine Top-Kandidaten ausgewählt.")
        return
    symbol = st.selectbox("Detailansicht", selected["Kürzel"].tolist())
    detail = selected[selected["Kürzel"] == symbol].iloc[0]
    features = json.loads(detail["features_json"])
    feature_rows = [
        {"Merkmal": feature_label(name), "Wert": format_feature(name, value)}
        for name, value in sorted(features.items(), key=lambda item: feature_label(item[0]))
    ]
    st.dataframe(pd.DataFrame(feature_rows), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    run()
