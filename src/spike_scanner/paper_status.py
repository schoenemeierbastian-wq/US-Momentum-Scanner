from __future__ import annotations

import json

from spike_scanner.config import Settings
from spike_scanner.paper_trading import PaperTradingConfig, PaperTradingStore


def main() -> None:
    settings = Settings.from_env(".env")
    config = PaperTradingConfig.from_env(settings)
    store = PaperTradingStore(config.database_path)
    print(
        json.dumps(
            {
                "mode": config.mode,
                "profile": config.profile,
                "database": str(store.path.resolve()),
                "limits": {
                    "entry_price": [
                        config.min_entry_price,
                        config.max_entry_price,
                    ],
                    "max_open_positions": config.max_open_positions,
                    "max_new_trades_per_day": config.max_new_trades_per_day,
                    "max_open_positions_by_phase": {
                        "PRE": config.max_open_positions_pre,
                        "RTH": config.max_open_positions_rth,
                        "ATH": config.max_open_positions_ath,
                    },
                    "priority_bands": {
                        "A": ">=75",
                        "B": "65-74.99",
                        "C": "55-64.99",
                    },
                    "min_purchase_score": config.min_purchase_score,
                    "cooldown_hours": config.cooldown_hours,
                },
                "entry_guard": {
                    "enabled": config.entry_guard_enabled,
                    "bar_count": config.entry_guard_bar_count,
                    "max_quote_age_seconds": config.entry_guard_max_quote_age_seconds,
                    "max_chase_pct": config.entry_guard_max_chase_pct,
                    "max_breakdown_pct": config.entry_guard_max_breakdown_pct,
                    "spread_limit_rth": config.top_mover_max_spread_rth,
                    "spread_limit_extended": config.top_mover_max_spread_extended,
                },
                "top_mover": {
                    "min_change_pct": config.top_mover_min_change_pct,
                    "min_relative_volume": config.top_mover_min_relative_volume,
                    "min_signal_score": config.top_mover_min_signal_score,
                    "max_risk_score": config.top_mover_max_risk_score,
                    "max_spread_rth": config.top_mover_max_spread_rth,
                    "max_spread_extended": config.top_mover_max_spread_extended,
                    "min_dollar_volume_5m": config.top_mover_min_dollar_volume_5m,
                    "candidates_per_scan": config.top_mover_candidates_per_scan,
                },
                **store.summary(),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
