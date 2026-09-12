from __future__ import annotations

import logging
from datetime import datetime, timezone

from spike_scanner.orderflow.metrics import measure_order_book
from spike_scanner.orderflow.models import OrderflowMeasurement, ScannerContext
from spike_scanner.orderflow.storage import OrderflowStorage
from spike_scanner.providers.webull_provider import is_transient_network_error

logger = logging.getLogger(__name__)


class OrderflowMonitor:
    """Getrennter Read-only-Monitor für Level-2-Snapshots."""

    def __init__(self, provider, storage: OrderflowStorage) -> None:
        self.provider = provider
        self.storage = storage

    def measure(self, context: ScannerContext, depth: int = 1) -> OrderflowMeasurement:
        # Der aktuell getestete Webull-Quotes-Endpunkt akzeptiert beim vorliegenden
        # Konto nur depth=1. Ein explizites Limit verhindert HTTP 417 bei alten
        # UI-Einstellungen. Mock-/spätere Provider dürfen weiterhin mehr Stufen.
        requested_depth = max(1, int(depth))
        effective_depth = 1 if str(getattr(self.provider, "name", "")).lower() == "webull" else requested_depth
        try:
            book = self.provider.get_order_book(context.symbol, depth=effective_depth)
            measurement = measure_order_book(
                book,
                context=context,
                provider=str(getattr(self.provider, "name", "unknown")),
                depth=effective_depth,
            )
            if effective_depth != requested_depth:
                measurement.warnings.append(
                    f"Webull-Tiefe automatisch von {requested_depth} auf 1 begrenzt."
                )
        except Exception as exc:
            if is_transient_network_error(exc):
                logger.warning(
                    "Orderflow-Messung für %s vorübergehend nicht erreichbar: %s",
                    context.symbol,
                    exc,
                )
            else:
                logger.exception("Orderflow-Messung für %s fehlgeschlagen", context.symbol)
            measurement = OrderflowMeasurement(
                symbol=context.symbol.upper(),
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                provider=str(getattr(self.provider, "name", "unknown")),
                depth_levels=effective_depth,
                data_quality="FEHLER",
                pressure="NICHT VERFÜGBAR",
                liquidity_status="UNBEKANNT",
                liquidity_reason="Messung fehlgeschlagen.",
                scan_run_id=context.run_id,
                scan_rank=context.rank,
                scanner_score=context.scanner_score,
                reference_price=context.reference_price,
                warnings=[str(exc)],
            )
        self.storage.save(measurement)
        return measurement

    def measure_many(
        self,
        contexts: list[ScannerContext],
        depth: int = 1,
    ) -> list[OrderflowMeasurement]:
        return [self.measure(context, depth=depth) for context in contexts]

    def measure_latest_top(self, limit: int = 10, depth: int = 1) -> list[OrderflowMeasurement]:
        return self.measure_many(self.storage.latest_top_contexts(limit=limit), depth=depth)
