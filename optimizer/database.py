from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .domain import Account, Sample


ACCOUNT_SQL = """
SELECT
    a.id,
    a.name,
    a.platform,
    a.concurrency,
    a.priority,
    a.load_factor,
    a.schedulable,
    a.status,
    ARRAY_AGG(DISTINCT ag.group_id ORDER BY ag.group_id) AS group_ids
FROM accounts AS a
JOIN account_groups AS ag ON ag.account_id = a.id
WHERE ag.group_id = ANY(%s)
  AND a.deleted_at IS NULL
  AND LOWER(a.platform) = 'openai'
GROUP BY a.id, a.name, a.platform, a.concurrency, a.priority,
         a.load_factor, a.schedulable, a.status
ORDER BY a.id
"""


REAL_SUCCESS_SQL = """
SELECT
    u.account_id,
    u.group_id,
    COALESCE(NULLIF(u.upstream_model, ''), NULLIF(u.model, ''), NULLIF(u.requested_model, '')) AS model,
    u.created_at AS occurred_at,
    u.first_token_ms AS ttft_ms
FROM usage_logs AS u
JOIN accounts AS a ON a.id = u.account_id
WHERE u.created_at >= NOW() - INTERVAL '24 hours'
  AND u.group_id = ANY(%s)
  AND a.deleted_at IS NULL
  AND LOWER(a.platform) = 'openai'
  AND u.first_token_ms IS NOT NULL
  AND u.first_token_ms > 0
  AND (
      u.request_type IN (2, 3)
      OR (u.request_type = 0 AND (u.stream IS TRUE OR u.openai_ws_mode IS TRUE))
  )
  AND COALESCE(u.image_count, 0) = 0
  AND COALESCE(u.video_count, 0) = 0
  AND COALESCE(u.billing_mode, 'token') = 'token'
  AND COALESCE(u.inbound_endpoint, '') NOT ILIKE '%%/images%%'
  AND COALESCE(u.inbound_endpoint, '') NOT ILIKE '%%/video%%'
  AND COALESCE(NULLIF(u.upstream_model, ''), NULLIF(u.model, ''), NULLIF(u.requested_model, '')) NOT ILIKE 'gpt-image-%%'
"""


REAL_FAILURE_SQL = """
SELECT DISTINCT ON (
    e.account_id,
    e.group_id,
    COALESCE(NULLIF(e.request_id, ''), e.id::text)
)
    e.account_id,
    e.group_id,
    COALESCE(NULLIF(e.upstream_model, ''), NULLIF(e.model, ''), NULLIF(e.requested_model, '')) AS model,
    e.created_at AS occurred_at,
    COALESCE(e.upstream_status_code, e.status_code) AS status_code,
    CASE
        WHEN COALESCE(e.upstream_status_code, e.status_code) IN (401, 403) THEN 'auth'
        WHEN COALESCE(e.upstream_status_code, e.status_code) = 429 THEN 'rate_limit'
        WHEN COALESCE(e.upstream_status_code, e.status_code) IN (408, 504, 524)
             OR COALESCE(e.network_error_type, '') ILIKE '%%timeout%%'
             OR COALESCE(e.error_type, '') ILIKE '%%timeout%%' THEN 'timeout'
        WHEN COALESCE(e.network_error_type, '') ILIKE '%%dns%%' THEN 'dns'
        WHEN COALESCE(e.network_error_type, '') ILIKE '%%tls%%' THEN 'tls'
        WHEN COALESCE(e.network_error_type, '') ILIKE '%%refused%%' THEN 'connection_refused'
        WHEN COALESCE(e.upstream_status_code, e.status_code) BETWEEN 500 AND 599 THEN 'server'
        ELSE 'upstream'
    END AS error_category
FROM ops_error_logs AS e
JOIN accounts AS a ON a.id = e.account_id
WHERE e.created_at >= NOW() - INTERVAL '24 hours'
  AND e.group_id = ANY(%s)
  AND e.account_id IS NOT NULL
  AND a.deleted_at IS NULL
  AND LOWER(a.platform) = 'openai'
  AND COALESCE(e.error_owner, '') = 'provider'
  AND COALESCE(e.error_phase, '') = 'upstream'
  AND COALESCE(e.error_type, '') <> 'cyber_policy'
  AND (
      e.request_type IN (2, 3)
      OR (COALESCE(e.request_type, 0) = 0 AND e.stream IS TRUE)
  )
  AND COALESCE(e.inbound_endpoint, '') NOT ILIKE '%%/images%%'
  AND COALESCE(e.inbound_endpoint, '') NOT ILIKE '%%/video%%'
  AND COALESCE(NULLIF(e.upstream_model, ''), NULLIF(e.model, ''), NULLIF(e.requested_model, '')) NOT ILIKE 'gpt-image-%%'
  AND NOT EXISTS (
      SELECT 1
      FROM usage_logs AS u
      WHERE u.request_id = e.request_id
        AND u.account_id = e.account_id
        AND u.first_token_ms IS NOT NULL
  )
ORDER BY
    e.account_id,
    e.group_id,
    COALESCE(NULLIF(e.request_id, ''), e.id::text),
    e.created_at DESC
"""


SETTINGS_SQL = """
SELECT key, value
FROM settings
WHERE key = 'openai_advanced_scheduler_enabled'
"""


GROUP_SQL = """
SELECT id, name, models_list_config, model_routing_enabled
FROM groups
WHERE id = ANY(%s) AND deleted_at IS NULL
ORDER BY id
"""


@dataclass(frozen=True)
class DatabaseSnapshot:
    captured_at: datetime
    accounts: list[Account]
    samples: list[Sample]
    settings: dict[str, str]
    group_metadata: dict[int, dict[str, Any]]
    enabled_scheduled_test_plans: int


class Database:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _connect(self, *, read_only: bool):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised in the container
            raise RuntimeError("psycopg is required to access PostgreSQL") from exc

        connection = psycopg.connect(
            host=self.config.database_host,
            port=self.config.database_port,
            dbname=self.config.database_name,
            user=self.config.database_user,
            password=self.config.database_password,
            application_name="sub2api-account-optimizer",
            options=f"-c statement_timeout={self.config.round_timeout_seconds * 1000}",
            row_factory=dict_row,
        )
        connection.read_only = read_only
        return connection

    def snapshot(self, *, read_only: bool = True) -> DatabaseSnapshot:
        if not read_only:
            raise ValueError(
                "optimizer telemetry snapshots must always use a read-only transaction"
            )
        group_ids = list(self.config.group_ids)
        with self._connect(read_only=True) as connection:
            with connection.transaction():
                accounts = [
                    self._account(row)
                    for row in connection.execute(ACCOUNT_SQL, (group_ids,))
                ]
                samples = [
                    self._success_sample(row)
                    for row in connection.execute(REAL_SUCCESS_SQL, (group_ids,))
                ]
                samples.extend(
                    self._failure_sample(row)
                    for row in connection.execute(REAL_FAILURE_SQL, (group_ids,))
                )
                settings = {
                    row["key"]: row["value"] for row in connection.execute(SETTINGS_SQL)
                }
                groups = {
                    int(row["id"]): {
                        "name": row["name"],
                        "models_list_config": row["models_list_config"],
                        "model_routing_enabled": row["model_routing_enabled"],
                    }
                    for row in connection.execute(GROUP_SQL, (group_ids,))
                }
                plans = connection.execute(
                    "SELECT COUNT(*) AS count FROM scheduled_test_plans WHERE enabled IS TRUE"
                ).fetchone()
        return DatabaseSnapshot(
            captured_at=datetime.now(timezone.utc),
            accounts=accounts,
            samples=samples,
            settings=settings,
            group_metadata=groups,
            enabled_scheduled_test_plans=int(plans["count"]),
        )

    @staticmethod
    def _account(row: dict[str, Any]) -> Account:
        return Account(
            id=int(row["id"]),
            name=str(row["name"]),
            group_ids=tuple(int(value) for value in row["group_ids"]),
            platform=str(row["platform"]),
            concurrency=int(row["concurrency"]),
            priority=int(row["priority"]),
            load_factor=int(row["load_factor"])
            if row["load_factor"] is not None
            else None,
            schedulable=bool(row["schedulable"]),
            status=str(row["status"]),
        )

    @staticmethod
    def _success_sample(row: dict[str, Any]) -> Sample:
        return Sample(
            account_id=int(row["account_id"]),
            group_id=int(row["group_id"]),
            model=str(row["model"] or "unknown"),
            source="real",
            occurred_at=row["occurred_at"],
            success=True,
            ttft_ms=int(row["ttft_ms"]),
        )

    @staticmethod
    def _failure_sample(row: dict[str, Any]) -> Sample:
        return Sample(
            account_id=int(row["account_id"]),
            group_id=int(row["group_id"]),
            model=str(row["model"] or "unknown"),
            source="real",
            occurred_at=row["occurred_at"],
            success=False,
            error_category=str(row["error_category"]),
            status_code=int(row["status_code"])
            if row["status_code"] is not None
            else None,
        )

    def scheduling_snapshot(self) -> list[dict[str, Any]]:
        group_ids = list(self.config.group_ids)
        with self._connect(read_only=True) as connection:
            rows = connection.execute(ACCOUNT_SQL, (group_ids,)).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "group_ids": [int(value) for value in row["group_ids"]],
                "concurrency": int(row["concurrency"]),
                "priority": int(row["priority"]),
                "load_factor": int(row["load_factor"])
                if row["load_factor"] is not None
                else None,
                "schedulable": bool(row["schedulable"]),
                "status": str(row["status"]),
            }
            for row in rows
        ]
