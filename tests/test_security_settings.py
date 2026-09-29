import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import (
    load_market_mode_gate_settings,
    load_risk_settings,
    load_settings,
    load_strategy_settings,
)
from kiwoom_rest_client import KiwoomRestClient


class SecuritySettingsTest(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"KIWOOM_APP_KEY": "", "KIWOOM_SECRET_KEY": ""},
        clear=False,
    )
    def test_credentials_must_come_from_environment(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "KIWOOM_APP_KEY"):
            load_settings()

    @patch.dict(
        os.environ,
        {
            "KIWOOM_APP_KEY": "app-key",
            "KIWOOM_SECRET_KEY": "secret-key",
            "KIWOOM_TOKEN_CACHE": "",
        },
        clear=False,
    )
    def test_token_cache_is_disabled_by_default(self) -> None:
        settings = load_settings()

        self.assertIsNone(settings.token_cache_path)

    def test_client_does_not_write_token_without_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KiwoomRestClient(
                "app-key",
                "secret-key",
                token_cache=None,
            )
            client._save_token_cache(
                {"token": "access-token", "expires_dt": "20990101000000"}
            )

            self.assertEqual(client.token, "access-token")
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    @patch.dict(
        os.environ,
        {
            "MARKET_MODE_GATE_ENABLED": "true",
            "MARKET_MODE_MANUAL_MODE": "off",
        },
        clear=False,
    )
    def test_market_mode_manual_alias_blocks_entry(self) -> None:
        settings = load_market_mode_gate_settings()

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.manual_mode, "risk_off")
        self.assertEqual(settings.requested_mode, "risk_off")
        self.assertEqual(settings.source, "manual_env")
        self.assertFalse(settings.entry_allowed)
        self.assertEqual(settings.entry_state, "blocked")

    @patch.dict(
        os.environ,
        {
            "MARKET_MODE_GATE_ENABLED": "true",
            "MARKET_MODE_MANUAL_MODE": "auto",
        },
        clear=False,
    )
    def test_market_mode_auto_fails_closed_until_data_is_connected(self) -> None:
        settings = load_market_mode_gate_settings()

        self.assertEqual(settings.requested_mode, "auto")
        self.assertEqual(settings.manual_mode, "risk_off")
        self.assertEqual(settings.source, "auto_unavailable")
        self.assertFalse(settings.entry_allowed)
        self.assertIn("fallback", settings.reason)

    @patch.dict(
        os.environ,
        {
            "MARKET_MODE_MANUAL_MODE": "auto",
            "MARKET_MODE_AUTO_FALLBACK_MODE": "invalid",
        },
        clear=False,
    )
    def test_market_mode_auto_rejects_invalid_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "MARKET_MODE_AUTO_FALLBACK_MODE"):
            load_market_mode_gate_settings()

    @patch.dict(
        os.environ,
        {
            "RISK_DAILY_MAX_LOSS_RATE": "",
            "RISK_MAX_CONSECUTIVE_LOSSES": "",
            "RISK_MAX_DAILY_STOP_LOSSES": "",
            "RISK_STOP_LOSS_COOLDOWN_MINUTES": "",
            "RISK_MAX_ENTRY_GAIN_RATE": "",
            "STRATEGY_MAX_DAY_GAIN_RATE": "",
            "STRATEGY_MORNING_ENTRY_GUARD_ENABLED": "",
            "STRATEGY_MORNING_MAX_NEW_BUYS": "",
            "STRATEGY_MORNING_MAX_A_GRADE_BUYS": "",
            "STRATEGY_MORNING_MAX_B_GRADE_BUYS": "",
            "STRATEGY_MAX_NEW_BUYS_PER_BUCKET": "",
            "STRATEGY_MAX_SAME_STOCK_BUYS_PER_DAY": "",
        },
        clear=False,
    )
    def test_default_paper_entry_policy_uses_account_loss_kill_switch_only(
        self,
    ) -> None:
        for key in list(os.environ):
            if key.startswith("RISK_") or key.startswith("STRATEGY_"):
                os.environ.pop(key, None)

        risk = load_risk_settings()
        strategy = load_strategy_settings()

        self.assertEqual(str(risk.daily_max_loss_rate), "0.10")
        self.assertEqual(risk.max_consecutive_losses, 0)
        self.assertEqual(risk.max_daily_stop_losses, 0)
        self.assertEqual(risk.stop_loss_cooldown_minutes, 0)
        self.assertEqual(str(risk.max_entry_gain_rate), "0")
        self.assertEqual(str(risk.max_position_rate), "1.0")
        self.assertEqual(str(strategy.max_day_gain_rate), "0")
        self.assertFalse(strategy.morning_entry_guard_enabled)
        self.assertEqual(strategy.morning_max_new_buys, 0)
        self.assertEqual(strategy.morning_max_a_grade_buys, 0)
        self.assertEqual(strategy.morning_max_b_grade_buys, 0)
        self.assertEqual(strategy.max_new_buys_per_bucket, 0)
        self.assertEqual(strategy.max_same_stock_buys_per_day, 0)


if __name__ == "__main__":
    unittest.main()
