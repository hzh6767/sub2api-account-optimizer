from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from optimizer.pricing import load_model_pricing, select_probe_model


class PricingTests(unittest.TestCase):
    def test_invalid_rates_fall_through_to_a_free_model(self) -> None:
        for rate in (-0.1, True, float("inf"), float("nan"), 10**400, 1e308):
            for field in ("input_cost_per_token", "output_cost_per_token"):
                with self.subTest(rate=rate, field=field):
                    free = {"input_cost_per_token": 0, "output_cost_per_token": 0}
                    prices = {"bad": {**free, field: rate}, "free": free}
                    chosen = select_probe_model(
                        {"bad", "free"}, ("bad", "free"),
                        prices,
                        input_tokens=2, output_tokens=2,
                    )
                    self.assertEqual(chosen.model, "free")
                    self.assertEqual(chosen.estimated_cost_usd, 0)
                    unpriced = select_probe_model(
                        {"bad"}, ("bad",), prices, input_tokens=2, output_tokens=2,
                    )
                    self.assertIsNone(unpriced.model)

    def test_invalid_catalog_encoding_is_treated_as_unpriced(self) -> None:
        with TemporaryDirectory() as directory:
            catalog = Path(directory) / "pricing.json"
            catalog.write_bytes(b"\xff")
            self.assertEqual(load_model_pricing(catalog), {})

    def test_selects_lowest_estimated_probe_cost_from_allowed_models(self) -> None:
        pricing = {
            "gpt-5.5": {
                "input_cost_per_token": 5e-6,
                "output_cost_per_token": 30e-6,
            },
            "gpt-5.6-terra": {
                "input_cost_per_token": 2.5e-6,
                "output_cost_per_token": 15e-6,
            },
            "gpt-5.6-sol": {
                "input_cost_per_token": 5e-6,
                "output_cost_per_token": 30e-6,
            },
        }

        selection = select_probe_model(
            {"gpt-5.5", "gpt-5.6-terra", "gpt-5.6-sol"},
            ("gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra"),
            pricing,
            input_tokens=1_700,
            output_tokens=1,
        )

        self.assertEqual("gpt-5.6-terra", selection.model)
        self.assertAlmostEqual(0.004265, selection.estimated_cost_usd)

    def test_missing_catalog_price_blocks_model_selection(self) -> None:
        selection = select_probe_model(
            {"gpt-5.5", "gpt-5.6-terra"},
            ("gpt-5.6-terra", "gpt-5.5"),
            {},
            input_tokens=1,
            output_tokens=1,
        )

        self.assertIsNone(selection.model)
        self.assertIsNone(selection.estimated_cost_usd)

    def test_partial_catalog_price_falls_through_to_priced_candidate(self) -> None:
        selection = select_probe_model(
            {"gpt-5.5", "gpt-5.6-terra"},
            ("gpt-5.6-terra", "gpt-5.5"),
            {
                "gpt-5.6-terra": {
                    "input_cost_per_token": 2.5e-6,
                    "output_cost_per_token": 15e-6,
                }
            },
            input_tokens=1,
            output_tokens=1,
        )

        self.assertEqual("gpt-5.6-terra", selection.model)


if __name__ == "__main__":
    unittest.main()
