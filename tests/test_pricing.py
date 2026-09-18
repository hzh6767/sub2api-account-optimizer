from __future__ import annotations

import unittest

from optimizer.pricing import select_probe_model


class PricingTests(unittest.TestCase):
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

    def test_partial_catalog_price_blocks_model_selection(self) -> None:
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

        self.assertIsNone(selection.model)


if __name__ == "__main__":
    unittest.main()
