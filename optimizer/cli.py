from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .api import AdminAPI
from .config import Config
from .database import Database
from .domain import RoundTimeoutError
from .engine import OptimizerEngine, scheduling_rollback_mutations
from .health import dependency_health
from .mutations import MutationExecutor
from .storage import JsonStore, sanitize


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - Windows development only
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(
                "another optimizer process already holds the lock"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            if os.name != "nt":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def round_timeout(seconds: int) -> Iterator[None]:
    if os.name == "nt" or not hasattr(signal, "SIGALRM"):
        yield
        return

    def timed_out(_signum: int, _frame: object) -> None:
        raise RoundTimeoutError("optimizer round exceeded its maximum execution time")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(max(1, seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _build_runtime(config: Config) -> tuple[OptimizerEngine, JsonStore, AdminAPI]:
    store = JsonStore(config.root)
    api = AdminAPI(
        config.api_base_url,
        targeted_test_safe=config.targeted_test_safe,
        admin_email=config.admin_email,
        admin_password=config.admin_password,
        admin_api_key_file=config.admin_api_key_file,
    )
    engine = OptimizerEngine(
        config,
        Database(config),
        api,
        store,
        dependency_checker=lambda: dependency_health(config),
    )
    return engine, store, api


def _summary(report: dict[str, object]) -> dict[str, object]:
    rankings = report.get("rankings", {})
    groups: dict[str, object] = {}
    if isinstance(rankings, dict):
        for group_id, rows in rankings.items():
            groups[str(group_id)] = [
                {
                    "rank": row.get("rank"),
                    "account_id": row.get("account_id"),
                    "account_name": row.get("account_name"),
                    "samples": row.get("sample_count"),
                    "score_ms": row.get("score_ms"),
                    "target_priority": row.get("target_priority"),
                    "target_load_factor": row.get("target_load_factor"),
                }
                for row in rows
                if isinstance(row, dict)
            ]
    return {
        "mode": report.get("mode"),
        "generated_at": report.get("generated_at"),
        "groups": groups,
        "warnings": report.get("warnings", []),
        "estimated_daily_probe_usage": report.get("estimated_daily_probe_usage", {}),
    }


def _load_json(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _status(store: JsonStore) -> int:
    path = store.logs_dir / "latest.json"
    if not path.exists():
        print(json.dumps({"status": "no-report"}))
        return 1
    print(json.dumps(_load_json(path), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def _healthcheck(store: JsonStore) -> int:
    path = store.logs_dir / "latest.json"
    if not path.exists():
        return 1
    age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
    return 0 if age <= 30 * 60 else 1


def _rollback(config: Config, store: JsonStore, api: AdminAPI) -> int:
    if not config.apply_enabled:
        print("rollback blocked: OPTIMIZER_APPLY_ENABLED is false", file=sys.stderr)
        return 2
    path = store.backups_dir / "activation-baseline.json"
    if not path.exists():
        print("rollback blocked: activation baseline is missing", file=sys.stderr)
        return 2
    baseline = _load_json(path)
    results = MutationExecutor(api, apply_enabled=True).execute(
        scheduling_rollback_mutations(baseline), dry_run=False
    )
    print(json.dumps(sanitize(results), ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if all(result.status == "applied" for result in results) else 1


def _daemon(config: Config, engine: OptimizerEngine) -> int:
    if not config.apply_enabled:
        print("daemon blocked: OPTIMIZER_APPLY_ENABLED is false", file=sys.stderr)
        return 2
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        cycle_started = time.monotonic()
        try:
            with round_timeout(config.round_timeout_seconds):
                report = engine.run(dry_run=False)
            print(
                json.dumps(_summary(report), ensure_ascii=True, sort_keys=True),
                flush=True,
            )
        except Exception as exc:
            print(
                json.dumps({"status": "error", "error_type": type(exc).__name__}),
                file=sys.stderr,
                flush=True,
            )
        remaining = max(
            0, config.loop_interval_seconds - (time.monotonic() - cycle_started)
        )
        while remaining > 0 and not stopping:
            delay = min(1.0, remaining)
            time.sleep(delay)
            remaining -= delay
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Sub2API OpenAI account optimizer")
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="read telemetry and preview decisions only",
    )
    modes.add_argument(
        "--once", action="store_true", help="run one enabled optimization cycle"
    )
    modes.add_argument("--status", action="store_true", help="print the latest report")
    modes.add_argument(
        "--rollback",
        action="store_true",
        help="restore the activation account-scheduling baseline",
    )
    modes.add_argument(
        "--daemon",
        action="store_true",
        help="run a light cycle every configured interval",
    )
    modes.add_argument("--healthcheck", action="store_true", help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = Config.from_env()
    engine, store, api = _build_runtime(config)
    if args.status:
        return _status(store)
    if args.healthcheck:
        return _healthcheck(store)

    with single_instance(store.state_dir / "optimizer.lock"):
        if args.dry_run:
            with round_timeout(config.round_timeout_seconds):
                report = engine.run(dry_run=True)
            print(
                json.dumps(
                    _summary(report), ensure_ascii=True, indent=2, sort_keys=True
                )
            )
            return 0
        if args.rollback:
            return _rollback(config, store, api)
        if args.daemon:
            return _daemon(config, engine)
        if not config.apply_enabled:
            print(
                "apply cycle blocked: OPTIMIZER_APPLY_ENABLED is false", file=sys.stderr
            )
            return 2
        with round_timeout(config.round_timeout_seconds):
            report = engine.run(dry_run=False)
        print(json.dumps(_summary(report), ensure_ascii=True, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
