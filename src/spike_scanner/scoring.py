from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-_clip(value, -30, 30)))


MIN_TOP_SIGNAL_SCORE = 25.0

# Kauf-Score 0.5.6: bewusst konservative Startwerte. Sie sind Forschungsregeln,
# keine Garantie und koennen spaeter aus den prospektiven 24-h-Daten optimiert werden.
PURCHASE_MIN_SIGNAL_COMMON = 50.0
PURCHASE_MAX_RISK_COMMON = 60.0
PURCHASE_MAX_SPREAD_COMMON = 0.04
PURCHASE_MIN_PROBABILITY_COMMON = 0.50

PURCHASE_MIN_SIGNAL_RTH = 70.0
PURCHASE_MAX_RISK_RTH = 40.0
PURCHASE_MAX_SPREAD_RTH = 0.03
PURCHASE_MIN_PROBABILITY_RTH = 0.55

PURCHASE_MIN_SIGNAL_EXTENDED = 80.0
PURCHASE_MAX_RISK_EXTENDED = 30.0
PURCHASE_MAX_SPREAD_EXTENDED = 0.02
PURCHASE_MIN_PROBABILITY_EXTENDED = 0.60


def is_top_eligible(signal_score: float, minimum: float = MIN_TOP_SIGNAL_SCORE) -> bool:
    """True, wenn die technische Signalstaerke fuer eine Top-Empfehlung ausreicht."""
    return float(signal_score) >= float(minimum)


def enforce_monotonic_probabilities(
    probabilities: dict[str, float] | None,
    thresholds: list[float] | tuple[float, ...],
) -> dict[str, float]:
    """Erzwingt P(+10) >= P(+20) >= P(+50) >= P(+100).

    Die Einzelmodelle duerfen intern weiterhin unabhaengig trainiert werden. Fuer Anzeige
    und Ranking werden hoehere Schwellen konservativ auf die zuletzt niedrigere
    Wahrscheinlichkeit begrenzt. Fehlende Modelle bleiben fehlend.
    """
    result = dict(probabilities or {})
    ceiling: float | None = None
    for threshold in sorted(float(t) for t in thresholds):
        key = f"{threshold:.6f}"
        value = result.get(key)
        if value is None:
            continue
        try:
            probability = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(probability):
            continue
        probability = _clip(probability, 0.0, 1.0)
        if ceiling is not None:
            probability = min(probability, ceiling)
        result[key] = probability
        ceiling = probability
    return result


def combined_score(signal_score: float, risk_score: float) -> float:
    """Risikoadjustierter Gesamt-Score (0-100), ausdruecklich keine Wahrscheinlichkeit.

    Der Signal-Score bleibt die Basis. Das Risiko kann den Score um hoechstens
    50 % reduzieren. So kann ein schwaches Signal nicht allein durch niedriges
    Risiko zu einem starken Gesamt-Score werden.
    """
    signal = _clip(float(signal_score), 0.0, 100.0)
    risk = _clip(float(risk_score), 0.0, 100.0)
    return round(signal * (1.0 - 0.5 * risk / 100.0), 2)


def ranking_score(
    signal_score: float,
    risk_score: float,
    model_probability: float | None,
) -> float:
    """Transparenter Ranking-Score (0-100), keine Kurswahrscheinlichkeit.

    Mit ML-Modell: 50 % Gesamt-Score, 30 % Modellwahrscheinlichkeit,
    20 % Signal-Score. Ohne Modell werden die verfuegbaren Komponenten
    renormiert: 70 % Gesamt-Score, 30 % Signal-Score.
    """
    signal = _clip(float(signal_score), 0.0, 100.0)
    overall = combined_score(signal, risk_score)
    probability: float | None
    try:
        probability = None if model_probability is None else float(model_probability)
    except (TypeError, ValueError):
        probability = None
    if probability is None or not math.isfinite(probability):
        return round(0.70 * overall + 0.30 * signal, 2)
    probability_pct = _clip(probability, 0.0, 1.0) * 100.0
    return round(0.50 * overall + 0.30 * probability_pct + 0.20 * signal, 2)


def _parse_utc(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _new_york_zone():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return timezone.utc


def market_phase_at(value: object) -> str:
    """PRE/RTH/ATH/OFF fuer einen UTC-/ISO-Zeitpunkt."""
    dt = _parse_utc(value)
    if dt is None:
        return "OFF"
    et = dt.astimezone(_new_york_zone())
    if et.weekday() >= 5:
        return "OFF"
    minute = et.hour * 60 + et.minute
    if 4 * 60 <= minute < 9 * 60 + 30:
        return "PRE"
    if 9 * 60 + 30 <= minute < 16 * 60:
        return "RTH"
    if 16 * 60 <= minute < 20 * 60:
        return "ATH"
    return "OFF"


def signal_age_minutes(first_seen_at: object, now: object | None = None) -> float:
    first = _parse_utc(first_seen_at)
    current = _parse_utc(now) if now is not None else datetime.now(timezone.utc)
    if first is None or current is None:
        return 0.0
    return max(0.0, (current - first).total_seconds() / 60.0)


def freshness_score(age_minutes: float | int | None) -> float:
    """0-100: frische Momentum-Signale werden bevorzugt, alte verlieren Gewicht."""
    try:
        age = max(0.0, float(age_minutes or 0.0))
    except (TypeError, ValueError):
        age = 0.0
    if age <= 5:
        return 100.0
    if age <= 15:
        return 100.0 - (age - 5.0) * 2.5  # 100 -> 75
    if age <= 30:
        return 75.0 - (age - 15.0) * (35.0 / 15.0)  # 75 -> 40
    if age <= 60:
        return 40.0 - (age - 30.0) * (40.0 / 30.0)  # 40 -> 0
    return 0.0


def execution_quality_score(features: dict[str, float] | None) -> float:
    """0-100 aus Spread und 5-Minuten-Dollarvolumen."""
    features = features or {}
    try:
        spread = max(0.0, float(features.get("spread_pct", 0.0) or 0.0))
    except (TypeError, ValueError):
        spread = 0.0
    try:
        liquidity = max(0.0, float(features.get("dollar_volume_5m", 0.0) or 0.0))
    except (TypeError, ValueError):
        liquidity = 0.0

    if spread <= 0.005:
        spread_score = 100.0
    elif spread <= 0.01:
        spread_score = 85.0
    elif spread <= 0.02:
        spread_score = 65.0
    elif spread <= 0.03:
        spread_score = 40.0
    elif spread <= 0.04:
        spread_score = 20.0
    else:
        spread_score = 0.0

    if liquidity >= 1_000_000:
        liquidity_score = 100.0
    elif liquidity >= 500_000:
        liquidity_score = 85.0
    elif liquidity >= 250_000:
        liquidity_score = 70.0
    elif liquidity >= 100_000:
        liquidity_score = 50.0
    elif liquidity >= 50_000:
        liquidity_score = 25.0
    else:
        liquidity_score = 10.0
    return round(0.5 * spread_score + 0.5 * liquidity_score, 2)


@dataclass(slots=True)
class PurchaseAssessment:
    score: float
    quality: str
    eligible: bool
    phase: str
    freshness: float
    execution_quality: float
    blockers: list[str]


def purchase_assessment(
    signal_score: float,
    risk_score: float,
    model_probability: float | None,
    features: dict[str, float] | None,
    *,
    age_minutes: float = 0.0,
    phase: str = "RTH",
) -> PurchaseAssessment:
    """Forschungs-Kauf-Score 0-100 mit harten Plausibilitaetsfiltern.

    Gewichtung:
      35 % Gesamt-Score
      25 % Wahrscheinlichkeit des primaeren 24-h-Ziels
      20 % Signal-Score
      10 % Signal-Frische
      10 % Ausfuehrungsqualitaet (Spread/Liquiditaet)

    Der Score ist keine Eintrittswahrscheinlichkeit und kein automatischer Kaufbefehl.
    PRE/ATH werden wegen typischerweise schlechterer Ausfuehrbarkeit strenger bewertet.
    """
    signal = _clip(float(signal_score), 0.0, 100.0)
    risk = _clip(float(risk_score), 0.0, 100.0)
    overall = combined_score(signal, risk)
    fresh = freshness_score(age_minutes)
    execution = execution_quality_score(features)

    probability: float | None
    try:
        probability = None if model_probability is None else float(model_probability)
    except (TypeError, ValueError):
        probability = None
    if probability is not None and not math.isfinite(probability):
        probability = None
    probability_pct = 0.0 if probability is None else _clip(probability, 0.0, 1.0) * 100.0

    score = round(
        0.35 * overall
        + 0.25 * probability_pct
        + 0.20 * signal
        + 0.10 * fresh
        + 0.10 * execution,
        2,
    )

    features = features or {}
    try:
        spread = max(0.0, float(features.get("spread_pct", 0.0) or 0.0))
    except (TypeError, ValueError):
        spread = 0.0

    phase = str(phase or "OFF").upper()
    blockers: list[str] = []
    if phase == "OFF":
        blockers.append("Marktphase OFF")
    if signal < PURCHASE_MIN_SIGNAL_COMMON:
        blockers.append(f"Signal < {PURCHASE_MIN_SIGNAL_COMMON:.0f}")
    if risk > PURCHASE_MAX_RISK_COMMON:
        blockers.append(f"Risiko > {PURCHASE_MAX_RISK_COMMON:.0f}")
    if spread > PURCHASE_MAX_SPREAD_COMMON:
        blockers.append(f"Spread > {PURCHASE_MAX_SPREAD_COMMON:.0%}")
    if probability is None:
        blockers.append("kein aktives Zielmodell")
    elif probability < PURCHASE_MIN_PROBABILITY_COMMON:
        blockers.append(f"Ziel-P < {PURCHASE_MIN_PROBABILITY_COMMON:.0%}")

    # Fuer die eigentliche Einstufung gelten strengere phasenspezifische Schwellen.
    phase_strict = True
    if phase == "RTH":
        phase_strict = (
            signal >= PURCHASE_MIN_SIGNAL_RTH
            and risk <= PURCHASE_MAX_RISK_RTH
            and spread <= PURCHASE_MAX_SPREAD_RTH
            and probability is not None
            and probability >= PURCHASE_MIN_PROBABILITY_RTH
        )
    elif phase in {"PRE", "ATH"}:
        phase_strict = (
            signal >= PURCHASE_MIN_SIGNAL_EXTENDED
            and risk <= PURCHASE_MAX_RISK_EXTENDED
            and spread <= PURCHASE_MAX_SPREAD_EXTENDED
            and probability is not None
            and probability >= PURCHASE_MIN_PROBABILITY_EXTENDED
        )
    else:
        phase_strict = False

    eligible = not blockers and phase_strict
    # Damit Zahl und Text nicht gegeneinander arbeiten, begrenzen harte Ausschluesse
    # auf <50 und phasenspezifisch noch nicht bestaetigte Faelle auf <65.
    if blockers:
        score = min(score, 49.0)
        quality = "AUSSCHLUSS"
    elif not phase_strict:
        score = min(score, 64.0)
        quality = "BEOBACHTEN"
    elif score >= 75.0 and probability_pct >= 65.0:
        quality = "SEHR STARK"
    elif score >= 65.0:
        quality = "STARK"
    else:
        quality = "BEOBACHTEN"

    return PurchaseAssessment(
        score=round(score, 2),
        quality=quality,
        eligible=eligible,
        phase=phase,
        freshness=round(fresh, 2),
        execution_quality=execution,
        blockers=blockers,
    )


@dataclass(slots=True)
class ScoreResult:
    signal_score: float
    risk_score: float
    reasons: list[str]
    contributions: dict[str, float]


def score_features(features: dict[str, float]) -> ScoreResult:
    """Transparenter heuristischer Score; ausdruecklich keine Wahrscheinlichkeit."""

    normalized = {
        "momentum_5m": _clip(features.get("ret_5m", 0.0) / 0.08, -1.5, 2.5),
        "momentum_15m": _clip(features.get("ret_15m", 0.0) / 0.18, -1.5, 2.5),
        "acceleration": _clip(features.get("momentum_acceleration", 0.0) / 0.06, -1.5, 2.0),
        "relative_volume": _clip(math.log1p(max(0.0, features.get("relative_volume_5m", 0.0))) / math.log(10), 0.0, 2.0),
        "volume_z": _clip(features.get("volume_z_20", 0.0) / 5.0, -1.0, 2.0),
        "breakout": _clip(features.get("breakout_20", 0.0) / 0.06, -1.5, 2.0),
        "vwap": _clip(features.get("vwap_distance", 0.0) / 0.12, -1.5, 2.0),
        "range_position": _clip((features.get("range_position", 0.5) - 0.5) * 2, -1.0, 1.0),
        "book_imbalance": _clip(features.get("book_imbalance", 0.0), -1.0, 1.0),
        "liquidity": _clip(math.log10(max(1.0, features.get("dollar_volume_5m", 1.0))) - 4.5, -1.5, 2.0),
        "screener_change": _clip(features.get("screener_change_ratio", 0.0) / 0.50, -1.0, 2.0),
        "screener_rvol": _clip(math.log1p(max(0.0, features.get("screener_relative_volume", 0.0))) / math.log(15), 0.0, 2.0),
    }
    weights = {
        "momentum_5m": 0.85,
        "momentum_15m": 0.75,
        "acceleration": 0.55,
        "relative_volume": 0.90,
        "volume_z": 0.45,
        "breakout": 0.70,
        "vwap": 0.45,
        "range_position": 0.30,
        "book_imbalance": 0.40,
        "liquidity": 0.35,
        "screener_change": 0.40,
        "screener_rvol": 0.45,
    }
    contributions = {key: normalized[key] * weights[key] for key in weights}
    raw = -2.1 + sum(contributions.values())

    spread_pct = max(0.0, features.get("spread_pct", 0.0))
    volatility = max(0.0, features.get("realized_volatility_20", 0.0))
    downside_vol = max(0.0, features.get("downside_volatility_20", 0.0))
    liquidity = max(1.0, features.get("dollar_volume_5m", 1.0))
    risk_raw = (
        2.2 * _clip(spread_pct / 0.04, 0.0, 2.0)
        + 1.6 * _clip(volatility / 0.04, 0.0, 2.0)
        + 1.0 * _clip(downside_vol / 0.025, 0.0, 2.0)
        + 1.2 * _clip((100_000 - liquidity) / 100_000, 0.0, 1.0)
        - 1.7
    )

    signal_score = round(_sigmoid(raw) * 100, 2)
    risk_score = round(_sigmoid(risk_raw) * 100, 2)

    labels = {
        "momentum_5m": "starkes 5-Minuten-Momentum",
        "momentum_15m": "starkes 15-Minuten-Momentum",
        "acceleration": "beschleunigendes Momentum",
        "relative_volume": "ungewoehnlich hohes Kurzfristvolumen",
        "volume_z": "Volumensprung gegenueber der Basis",
        "breakout": "Ausbruch ueber das 20-Minuten-Hoch",
        "vwap": "Kurs deutlich ueber VWAP",
        "range_position": "Kurs nahe am Intraday-Hoch",
        "book_imbalance": "Orderbuch zugunsten der Geldseite",
        "liquidity": "ausreichendes Dollarvolumen",
        "screener_change": "bereits hoher Tagesanstieg",
        "screener_rvol": "hohes Screener-Relativvolumen",
    }
    strongest = sorted(contributions.items(), key=lambda item: item[1], reverse=True)
    reasons = [labels[key] for key, value in strongest if value > 0.18][:5]
    if spread_pct > 0.03:
        reasons.append("Warnung: grosser Bid/Ask-Spread")
    if risk_score > 75:
        reasons.append("Warnung: sehr hohes Ausfuehrungsrisiko")
    return ScoreResult(signal_score, risk_score, reasons, contributions)
