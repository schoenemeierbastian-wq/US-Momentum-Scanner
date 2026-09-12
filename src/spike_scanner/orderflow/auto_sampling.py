from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AutoSamplingState:
    """Kleiner, UI-unabhängiger Zustand für automatische Messreihen."""

    active: bool = False
    interval_seconds: int = 10
    completed_cycles: int = 0

    @staticmethod
    def normalize_interval(value: int | str, minimum: int = 5, maximum: int = 300) -> int:
        try:
            interval = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Das Messintervall muss eine ganze Zahl sein.") from exc
        if interval < minimum or interval > maximum:
            raise ValueError(
                f"Das Messintervall muss zwischen {minimum} und {maximum} Sekunden liegen."
            )
        return interval

    def start(self, interval_seconds: int | str) -> None:
        self.interval_seconds = self.normalize_interval(interval_seconds)
        self.completed_cycles = 0
        self.active = True

    def stop(self) -> None:
        self.active = False

    def mark_completed(self) -> int:
        self.completed_cycles += 1
        return self.completed_cycles
