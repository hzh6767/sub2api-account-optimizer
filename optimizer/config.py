from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value and value.strip() else default


@dataclass(frozen=True)
class Config:
    root: Path
    group_ids: tuple[int, ...]
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password: str
    api_base_url: str
    apply_enabled: bool
    active_probes_enabled: bool
    targeted_test_safe: bool
    loop_interval_seconds: int
    round_timeout_seconds: int
    probe_model_preference: tuple[str, ...]
    admin_email: str
    admin_password: str
    admin_api_key_file: Path | None
    model_pricing_file: Path | None = None
    probe_input_tokens_estimate: int = 16
    probe_output_tokens_estimate: int = 1

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.getenv("OPTIMIZER_ROOT", "/app"))
        groups = tuple(
            int(value.strip())
            for value in os.getenv("OPTIMIZER_GROUP_IDS", "7,59").split(",")
            if value.strip()
        )
        key_file = os.getenv("SUB2API_ADMIN_API_KEY_FILE", "").strip()
        pricing_file = os.getenv(
            "SUB2API_MODEL_PRICING_FILE", "/app/config/model_pricing.json"
        ).strip()
        return cls(
            root=root,
            group_ids=groups,
            database_host=os.getenv("POSTGRES_HOST", "postgres"),
            database_port=_int("DATABASE_PORT", 5432),
            database_name=os.getenv("POSTGRES_DB", "sub2api"),
            database_user=os.getenv("POSTGRES_USER", "sub2api"),
            database_password=os.getenv("POSTGRES_PASSWORD", ""),
            api_base_url=os.getenv("SUB2API_ADMIN_BASE_URL", "http://sub2api:8080"),
            apply_enabled=_bool("OPTIMIZER_APPLY_ENABLED", False),
            active_probes_enabled=_bool("OPTIMIZER_ACTIVE_PROBES_ENABLED", False),
            targeted_test_safe=_bool("OPTIMIZER_TARGETED_TEST_SAFE", False),
            loop_interval_seconds=_int("OPTIMIZER_LOOP_INTERVAL_SECONDS", 600),
            round_timeout_seconds=_int("OPTIMIZER_ROUND_TIMEOUT_SECONDS", 480),
            probe_model_preference=tuple(
                value.strip()
                for value in os.getenv(
                    "OPTIMIZER_PROBE_MODEL_PREFERENCE",
                    "gpt-5.6-terra,gpt-5.6-sol,gpt-5.5",
                ).split(",")
                if value.strip()
            ),
            admin_email=os.getenv("ADMIN_EMAIL", ""),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            admin_api_key_file=Path(key_file) if key_file else None,
            model_pricing_file=Path(pricing_file) if pricing_file else None,
            probe_input_tokens_estimate=_int(
                "OPTIMIZER_PROBE_INPUT_TOKENS_ESTIMATE", 16
            ),
            probe_output_tokens_estimate=_int(
                "OPTIMIZER_PROBE_OUTPUT_TOKENS_ESTIMATE", 1
            ),
        )
