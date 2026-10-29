"""Storage routing proof on synthetic paths; no production processes or data."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.runtime_epoch.watch_state import operational_watch_state_path
from app.storage.database import connect_database
from scripts import run_scan, run_telegram_bot
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY, bootstrap_operational_test_database
from tests.test_watch_mode import SequenceWatchRunner, _rejected_symbol

pytestmark = pytest.mark.no_auto_epoch


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_scan, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_telegram_bot, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("RUNTIME_STATE_ROOT", raising=False)
    for name, value in {
        "TELEGRAM_ADMIN_ENABLED": "false", "TELEGRAM_DRY_RUN": "true",
        "TELEGRAM_SIGNALS_ENABLED": "false", "LOCAL_MANUAL_MODE": "true",
        "ORDER_EXECUTION_ENABLED": "false", "CMC_MODE": "off",
    }.items():
        monkeypatch.setenv(name, value)
    for field in ("epoch_id", "cutoff_at", "reviewed_release_sha", "generation_binding", "contract_version"):
        env = "RUNTIME_EPOCH_CONTRACT_VERSION" if field == "contract_version" else {
            "cutoff_at": "RUNTIME_EPOCH_CUTOFF_AT", "epoch_id": "RUNTIME_EPOCH_ID",
        }.get(field, "RUNTIME_" + field.upper())
        monkeypatch.setenv(env, getattr(SYNTHETIC_IDENTITY, field))
    legacy = tmp_path / "scan_runs"
    legacy.mkdir()
    for name, filename in {**run_scan._STATE_FILENAMES, "WATCH_STATE_PATH": "watch_state.json"}.items():
        path = legacy / filename
        if name == "ADMIN_DRAFTS_DIR":
            path.mkdir()
            (path / "keep.txt").write_bytes(b"legacy draft")
        else:
            path.write_bytes(b"legacy state must not be read or changed")
        monkeypatch.setattr(run_scan, name, path)
    return tmp_path / "v2", legacy


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def configured(root, *options):
    args = run_scan.parse_args(["--runtime-state-root", str(root), *options])
    run_scan._configure_runtime_state_paths(args, Settings(_env_file=None))
    return args


def directory_link(link, target):
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_defaults_and_explicit_state_paths_are_rooted_without_creating_state(isolated):
    root, legacy = isolated
    before = snapshot(legacy)
    args = configured(root, "--save-run", "--export-report", "--export-json", "--export-watchlist",
                      "--research-output-json", "--cache-file", "cache.json", "--resume-from", "resume.json",
                      "--watch-output-file", "watch.jsonl", "--output-json", "output.json")
    assert args.database_path == root / "candle_craft.db"
    for field in ("save_run", "export_report", "export_json", "export_watchlist", "research_output_json",
                  "cache_file", "resume_from", "watch_output_file", "output_json"):
        assert getattr(args, field).is_relative_to(root)
    for name, filename in run_scan._STATE_FILENAMES.items():
        assert run_scan._runtime_path(args, name) == root / filename
    assert args.watch_state_base_dir == root
    assert not root.exists()
    assert snapshot(legacy) == before


def test_environment_root_and_cli_precedence(isolated, monkeypatch):
    root, _ = isolated
    monkeypatch.setenv("RUNTIME_STATE_ROOT", str(root))
    args = run_scan.parse_args([])
    run_scan._configure_runtime_state_paths(args, Settings(_env_file=None))
    assert args.database_path == root / "candle_craft.db"
    other = configured(root.parent / "other")
    assert other.database_path == root.parent / "other" / "candle_craft.db"


def test_omitting_root_preserves_existing_paths(isolated):
    args = run_scan.parse_args([])
    original_db = args.database_path
    run_scan._configure_runtime_state_paths(args, Settings(_env_file=None))
    assert args.database_path == original_db
    assert run_scan._runtime_path(args, "SCAN_RUN_MANIFEST_PATH") == run_scan.SCAN_RUN_MANIFEST_PATH
    assert not hasattr(args, "runtime_state_paths")


@pytest.mark.parametrize("option", [
    "--database-path", "--save-run", "--cache-file", "--resume-from", "--watch-output-file",
    "--output-json", "--export-report", "--export-json", "--export-watchlist",
    "--research-output-json", "--backtest-output-json", "--edge-export-json", "--export-history-json",
])
def test_explicit_escape_fails_before_lease_or_database_access(isolated, monkeypatch, option):
    root, legacy = isolated
    before = snapshot(legacy)
    def forbidden(*args, **kwargs):
        pytest.fail("storage/acquisition started before path validation")
    monkeypatch.setattr(run_scan, "WatchSession", forbidden)
    monkeypatch.setattr(run_scan, "_main", forbidden)
    with pytest.raises(SystemExit, match="escapes configured root"):
        asyncio.run(run_scan.main(["--runtime-state-root", str(root), "--watch",
                                   option, str(legacy / "candle_craft.db")]))
    assert not root.exists()
    assert snapshot(legacy) == before


@pytest.mark.parametrize("root_kind", ["legacy", "legacy_child", "legacy_parent", "legacy_alias", "file"])
def test_unsafe_roots_are_rejected_without_writes(isolated, root_kind):
    root, legacy = isolated
    candidate = {"legacy": legacy, "legacy_child": legacy / "child", "legacy_parent": legacy.parent}.get(root_kind)
    if root_kind == "legacy_alias":
        directory_link(root, legacy)
        candidate = root
    if root_kind == "file":
        root.write_bytes(b"keep file")
        candidate = root
    before = snapshot(legacy)
    with pytest.raises(SystemExit, match="Invalid runtime state paths"):
        configured(candidate)
    assert snapshot(legacy) == before


@pytest.mark.parametrize("child", ["admin_drafts", "epochs"])
def test_default_child_junction_cannot_redirect_state_into_legacy(isolated, child):
    root, legacy = isolated
    root.mkdir()
    directory_link(root / child, legacy)
    before = snapshot(legacy)
    with pytest.raises(SystemExit, match="escapes configured root"):
        configured(root)
    assert snapshot(legacy) == before


def test_explicit_path_through_junction_is_rejected(isolated):
    root, legacy = isolated
    root.mkdir()
    directory_link(root / "alias", legacy)
    before = snapshot(legacy)
    with pytest.raises(SystemExit, match="escapes configured root"):
        configured(root, "--output-json", "alias/output.json")
    assert snapshot(legacy) == before


@pytest.mark.parametrize("watch", [False, True])
def test_real_main_writes_only_new_generation_and_does_not_rebind_globals(isolated, monkeypatch, watch):
    root, legacy = isolated
    before = snapshot(legacy)
    globals_before = {name: getattr(run_scan, name) for name in (*run_scan._STATE_FILENAMES, "WATCH_STATE_PATH")}
    bootstrap_operational_test_database(root / "candle_craft.db")
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_rejected_symbol(),)]
    drafts = []
    original_route = run_scan.route_admin_scan_report
    async def route(*args, **kwargs):
        drafts.append(kwargs["drafts_dir"])
        return await original_route(*args, **kwargs)
    monkeypatch.setattr(run_scan, "route_admin_scan_report", route)
    options = ["--symbols", "BTCUSDT", "--runtime-state-root", str(root), "--store-scan", "--save-run",
               "--output-json", "output.json", "--export-report", "--export-json", "--export-watchlist"]
    if watch:
        options += ["--watch", "--watch-interval-sec", "0.01", "--watch-max-iterations", "2"]
    else:
        options += ["--performance-memory"]
    asyncio.run(run_scan.main(options))
    rows = [json.loads(line) for line in (root / "scan_run_manifest.jsonl").read_text().splitlines()]
    assert len(rows) == (2 if watch else 1)
    assert (root / "nightly_scan_history.json").is_file()
    assert drafts == [root / "admin_drafts"] * len(rows)
    assert (root / "latest_scan.json").is_file()
    with connect_database(root / "candle_craft.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == len(rows)
    if watch:
        assert operational_watch_state_path(SYNTHETIC_IDENTITY.epoch_id, base_dir=root).is_file()
        receipt = json.loads(Path(f"{root / 'candle_craft.db'}.watch-session.json").read_text())
        assert receipt["shutdown_outcome"] == "COMPLETED"
        assert receipt["latest_iteration"]["completion_observed"] is True
    else:
        assert (root / "performance_memory.json").is_file()
    assert snapshot(legacy) == before
    assert {name: getattr(run_scan, name) for name in globals_before} == globals_before


def test_one_shot_monitoring_failure_diagnostic_is_isolated(isolated, monkeypatch):
    root, legacy = isolated
    bootstrap_operational_test_database(root / "candle_craft.db")
    before = snapshot(legacy)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_rejected_symbol(),)]
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic monitoring failure")
    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", fail)
    with pytest.raises(SystemExit, match="synthetic monitoring failure"):
        asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--runtime-state-root", str(root), "--lifecycle"]))
    row = json.loads((root / "scan_run_manifest.jsonl").read_text())
    assert row["record_kind"] == "owner_monitoring_failure_diagnostic"
    assert row["scan_completion_record"] is False
    assert snapshot(legacy) == before


def test_discovery_failure_diagnostic_is_isolated(isolated, monkeypatch):
    root, legacy = isolated
    before = snapshot(legacy)
    args = configured(root)
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic monitoring failure")
    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", fail)
    asyncio.run(run_scan._surface_monitoring_during_discovery_failure(args, RuntimeError("synthetic discovery")))
    row = json.loads((root / "scan_run_manifest.jsonl").read_text())
    assert row["record_kind"] == "discovery_failure_monitoring_diagnostic"
    assert row["scan_completion_record"] is False
    assert snapshot(legacy) == before


def test_one_shot_telegram_failure_diagnostic_is_isolated(isolated, monkeypatch):
    root, legacy = isolated
    bootstrap_operational_test_database(root / "candle_craft.db")
    before = snapshot(legacy)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_rejected_symbol(),)]
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic delivery failure")
    monkeypatch.setattr(run_scan, "_deliver_telegram_manual_signals_if_enabled", fail)
    with pytest.raises(RuntimeError, match="synthetic delivery failure"):
        asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--runtime-state-root", str(root)]))
    row = json.loads((root / "scan_run_manifest.jsonl").read_text())
    assert row["record_kind"] == "telegram_provenance_failure_diagnostic"
    assert row["scan_completion_record"] is False
    assert snapshot(legacy) == before


@pytest.mark.parametrize("use_environment", [False, True])
def test_listener_receives_same_namespace_without_polling(isolated, monkeypatch, use_environment):
    root, legacy = isolated
    before = snapshot(legacy)
    captured = {}
    class Service:
        def __init__(self, **kwargs):
            captured.update(kwargs)
    async def poll(**kwargs):
        captured.update(kwargs)
    monkeypatch.setattr(run_telegram_bot, "TelegramAdminCommandService", Service)
    monkeypatch.setattr(run_telegram_bot, "run_polling_loop", poll)
    if use_environment:
        monkeypatch.setenv("RUNTIME_STATE_ROOT", str(root))
        options = []
    else:
        options = ["--runtime-state-root", str(root)]
    assert run_telegram_bot.main(options + ["--dry-run"], settings=Settings(
        _env_file=None, telegram_commands_enabled=True,
    )) == 0
    for field, suffix in {"database_path": "candle_craft.db", "manifest_path": "scan_run_manifest.jsonl",
                          "state_path": "admin_commands/state.json", "audit_path": "admin_commands/commands.jsonl"}.items():
        assert captured[field] == root / suffix
    assert captured["dry_run"] is True
    assert not root.exists()
    assert snapshot(legacy) == before


@pytest.mark.parametrize("option", ["--database-path", "--manifest-path", "--state-path", "--audit-path"])
def test_listener_rejects_legacy_paths_before_service_construction(isolated, monkeypatch, option):
    root, legacy = isolated
    def forbidden(**kwargs):
        pytest.fail("listener service constructed with an unsafe path")
    monkeypatch.setattr(run_telegram_bot, "TelegramAdminCommandService", forbidden)
    with pytest.raises(SystemExit, match="escapes configured root"):
        run_telegram_bot.main(["--runtime-state-root", str(root), option, str(legacy / "old.db")],
                              settings=Settings(_env_file=None))


def test_paths_for_two_invocations_remain_independent(isolated):
    root, _ = isolated
    first = configured(root)
    second = configured(root.parent / "other")
    assert run_scan._runtime_path(first, "SCAN_RUN_MANIFEST_PATH") == root / "scan_run_manifest.jsonl"
    assert run_scan._runtime_path(second, "SCAN_RUN_MANIFEST_PATH") == root.parent / "other/scan_run_manifest.jsonl"
