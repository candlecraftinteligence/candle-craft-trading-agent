from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.formatters.scanner_display import rank_scan_results
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult
from app.research.queries import build_research_report
from app.storage import database as database_module
from app.storage import repositories as repositories_module
from app.storage.database import SCHEMA_VERSION, StorageError, open_initialized_database, open_read_only_database
from app.storage.models import WatchIterationMetadata
from app.storage.repositories import _json_dump, _json_ready, _storage_payload, export_history_payload, store_scan_result
from app.storage.scan_payloads import (
    FALLBACK_DUPLICATE_SYMBOLS,
    FALLBACK_INLINE_REQUESTED,
    FALLBACK_PAYLOAD_CHILD_BYTES_MISMATCH,
    FALLBACK_RESULTS_EMPTY,
    FALLBACK_RESULTS_MISSING,
    FALLBACK_RESULTS_NOT_LIST,
    FALLBACK_SYMBOL_SET_MISMATCH,
    INLINE_V1,
    SYMBOL_REFS_V1,
    ScanPayloadIntegrityError,
    encode_scan_raw_payload,
    inspect_physical_scan_payload,
    load_logical_scan_payload,
    sha256_utf8,
)
from app.telegram_admin.wolf_briefing import load_latest_db_scan_artifacts
from scripts import run_scan
from tests.test_storage_database import (
    _near_miss_symbol,
    _rejected_symbol,
    _scan_result,
    _valid_symbol,
)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _data_issue_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="ADAUSDT",
        status=ScannerPipelineStatus.SCAN_ERROR,
        status_history=(ScannerPipelineStatus.SCAN_ERROR,),
        error_message="Candle fetch failed.",
        rejection_reason="Candle fetch failed.",
        iteration_outcome="errored",
    )


def _not_run_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="SOLUSDT",
        status=ScannerPipelineStatus.NOT_RUN,
        status_history=(ScannerPipelineStatus.NOT_RUN,),
        iteration_outcome="not_run",
        not_run_reason="global_timeout_not_run",
        error_message="Symbol was not run: global_timeout_not_run.",
    )


def _nested_valid_symbol() -> ScannerSymbolResult:
    base = _valid_symbol()
    diagnostics = dict(base.strategy_diagnostics)
    diagnostics["scalp"] = {
        "is_valid": False,
        "mode": "scalp",
        "first_failed_gate": "missing_confirmed_sweep",
        "unverified": "Unverified",
        "missing": "N/A",
    }
    return base.model_copy(update={"strategy_diagnostics": diagnostics})


def _diverse_scan_result() -> ScannerRunResult:
    result = _scan_result()
    return result.model_copy(
        update={
            "results": (
                _nested_valid_symbol(),
                _near_miss_symbol(),
                _rejected_symbol(),
                _data_issue_symbol(),
                _not_run_symbol(),
            ),
            "scanned_symbols": 5,
        }
    )


def _watch_meta() -> WatchIterationMetadata:
    return WatchIterationMetadata(
        iteration_number=2,
        started_at="2026-09-08T00:00:00+00:00",
        completed_at="2026-09-08T00:00:02+00:00",
        symbols_requested=3,
        symbols_queued=3,
        symbols_completed=3,
        valid_activations=1,
        still_watching=2,
        rejected_no_edge=0,
        data_issues=0,
        runtime_sec=1.5,
        portfolio_summary={"selected_count": 0},
        symbol_health_summary={"BTCUSDT": "ok"},
    )


def _oracle_payload(result: ScannerRunResult, *, cli: bool = False, watch: bool = False) -> dict:
    ranked = rank_scan_results(result.results)
    if cli or watch:
        payload = run_scan._json_payload(result, ranked_results=ranked)
        if watch:
            payload["watch_iteration"] = {"iteration": 2, "note": "keep"}
            payload["watch_iteration_storage"] = {"watch_iteration_number": 2}
        return _json_ready(payload)
    ranked_by_symbol = {item.symbol_result.symbol: item for item in ranked}
    return _json_ready(_storage_payload(result, ranked_by_symbol, None, None))


def _store(
    db_path: Path,
    result: ScannerRunResult,
    *,
    raw_payload: dict | None = None,
    inline: bool = False,
    watch: bool = False,
    run_id: str | None = None,
) -> str:
    ranked = rank_scan_results(result.results)
    return store_scan_result(
        db_path,
        result,
        ranked_results=ranked,
        raw_payload=raw_payload,
        watch_iteration=_watch_meta() if watch else None,
        inline_raw_payload=inline,
        run_id=run_id,
    )


def _physical(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT raw_payload_json, raw_payload_format, total_valid_setups, near_misses, rejected,
               data_issues, actionable_a_grade_setups, confirmed_setups, candidate_a_grade_setups,
               is_watch_iteration, watch_iteration_number, valid_activations, still_watching,
               runtime_stats_json, portfolio_summary_json, symbol_health_summary_json
        FROM scan_runs WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    assert row is not None
    return row


def _child_map(connection: sqlite3.Connection, run_id: str) -> dict[str, str]:
    rows = connection.execute(
        "SELECT symbol, raw_result_json FROM symbol_results WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _write_v24_fixture(path: Path, *, run_id: str, payload: dict, children: dict[str, str]) -> None:
    payload_json = _json_dump(payload)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE scan_runs (
                run_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL DEFAULT '2026-09-01T00:00:00+00:00',
                exchange TEXT NOT NULL DEFAULT 'binance',
                universe TEXT NOT NULL DEFAULT 'test',
                symbols_scanned INTEGER NOT NULL DEFAULT 0,
                symbols_json TEXT NOT NULL DEFAULT '[]',
                strategy TEXT NOT NULL DEFAULT 'test',
                timeframes_json TEXT NOT NULL DEFAULT '{}',
                market_regime TEXT NOT NULL DEFAULT 'N/A',
                regime_confidence INTEGER NOT NULL DEFAULT 0,
                regime_compatibility_json TEXT NOT NULL DEFAULT '{}',
                environment_notes_json TEXT NOT NULL DEFAULT '[]',
                runtime_stats_json TEXT NOT NULL DEFAULT '{}',
                command_preset TEXT NOT NULL DEFAULT 'N/A',
                command_used TEXT NOT NULL DEFAULT 'N/A',
                total_valid_setups INTEGER NOT NULL DEFAULT 0,
                near_misses INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                data_issues INTEGER NOT NULL DEFAULT 0,
                data_issues_json TEXT NOT NULL DEFAULT '[]',
                raw_payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE symbol_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'N/A',
                display_bucket TEXT NOT NULL DEFAULT 'N/A',
                readiness_score INTEGER NOT NULL DEFAULT 0,
                setup_quality_score TEXT NOT NULL DEFAULT 'N/A',
                edge_score TEXT NOT NULL DEFAULT 'N/A',
                failed_gate TEXT NOT NULL DEFAULT 'N/A',
                rejection_reason TEXT NOT NULL DEFAULT 'N/A',
                next_trigger_needed TEXT NOT NULL DEFAULT 'N/A',
                action_label TEXT NOT NULL DEFAULT 'N/A',
                regime_state TEXT NOT NULL DEFAULT 'N/A',
                regime_confidence TEXT NOT NULL DEFAULT 'N/A',
                regime_compatibility_score TEXT NOT NULL DEFAULT 'N/A',
                regime_compatibility_label TEXT NOT NULL DEFAULT 'N/A',
                regime_penalty INTEGER NOT NULL DEFAULT 0,
                environment_notes_json TEXT NOT NULL DEFAULT '[]',
                derivatives_context_json TEXT NOT NULL DEFAULT '{}',
                volume_profile_context_json TEXT NOT NULL DEFAULT '{}',
                pullback_status TEXT NOT NULL DEFAULT 'N/A',
                portfolio_decision TEXT NOT NULL DEFAULT 'N/A',
                raw_result_json TEXT NOT NULL,
                UNIQUE(run_id, symbol)
            )
            """
        )
        connection.execute(
            "INSERT INTO scan_runs (run_id, raw_payload_json) VALUES (?, ?)",
            (run_id, payload_json),
        )
        for symbol, raw in children.items():
            connection.execute(
                """
                INSERT INTO symbol_results (
                    run_id, symbol, raw_result_json
                ) VALUES (?, ?, ?)
                """,
                (run_id, symbol, raw),
            )
        connection.execute("PRAGMA user_version = 24")
        connection.commit()


def test_current_application_schema_is_v25_after_storage_single_copy() -> None:
    # Deliberate additive STORAGE_SINGLE_COPY migration; historical v24 fixtures stay readable.
    assert SCHEMA_VERSION == 25


def test_ordinary_default_cli_and_watch_use_symbol_refs_with_exact_reconstruction(tmp_path: Path) -> None:
    result = _diverse_scan_result()
    families = (
        ("default", False, False, False),
        ("cli", True, False, False),
        ("watch", True, True, True),
    )
    reports = []
    for name, cli, watch, store_watch in families:
        oracle = _oracle_payload(result, cli=cli, watch=watch)
        db_path = tmp_path / f"{name}.sqlite"
        run_id = _store(db_path, result, raw_payload=oracle if cli or watch else None, watch=store_watch)
        with open_read_only_database(db_path) as connection:
            physical = _physical(connection, run_id)
            children = _child_map(connection, run_id)
            loaded = load_logical_scan_payload(connection, run_id)
            inspection = inspect_physical_scan_payload(connection, run_id)
        assert physical["raw_payload_format"] == SYMBOL_REFS_V1
        assert inspection.encoding == SYMBOL_REFS_V1
        assert inspection.reference_count == len(oracle["results"])
        assert _json_dump(loaded) == _json_dump(oracle)
        assert [item["symbol"] for item in loaded["results"]] == [item["symbol"] for item in oracle["results"]]
        compact_results = json.loads(physical["raw_payload_json"])["results"]
        assert all(set(item) == {"symbol", "sha256"} for item in compact_results)
        for item, compact in zip(oracle["results"], compact_results, strict=True):
            child = children[item["symbol"]]
            assert _json_dump(item) == child
            assert compact["sha256"] == sha256_utf8(child)
        assert inspection.raw_payload_utf8_bytes < inspection.child_raw_utf8_bytes
        assert inspection.raw_payload_utf8_bytes < _utf8_len(_json_dump(oracle))
        reports.append(
            {
                "family": name,
                "format": physical["raw_payload_format"],
                "inline_utf8": _utf8_len(_json_dump(oracle)),
                "physical_utf8": _utf8_len(physical["raw_payload_json"]),
                "child_utf8": sum(_utf8_len(value) for value in children.values()),
            }
        )
        if store_watch:
            assert physical["is_watch_iteration"] == 1
            assert physical["watch_iteration_number"] == 2
            assert physical["valid_activations"] == 1
            assert physical["still_watching"] == 2
            assert json.loads(physical["portfolio_summary_json"])["selected_count"] == 0
            assert "watch_iteration" in loaded
    assert all(item["physical_utf8"] < item["inline_utf8"] for item in reports)
    assert all(item["format"] == SYMBOL_REFS_V1 for item in reports)


def test_summaries_and_child_bytes_match_inline_oracle(tmp_path: Path) -> None:
    result = _diverse_scan_result()
    oracle = _oracle_payload(result)
    compact_id = _store(tmp_path / "compact.sqlite", result)
    inline_id = _store(tmp_path / "inline.sqlite", result, inline=True)
    with open_read_only_database(tmp_path / "compact.sqlite") as compact_conn, open_read_only_database(
        tmp_path / "inline.sqlite"
    ) as inline_conn:
        compact_row = _physical(compact_conn, compact_id)
        inline_row = _physical(inline_conn, inline_id)
        compact_children = _child_map(compact_conn, compact_id)
        inline_children = _child_map(inline_conn, inline_id)
        loaded = load_logical_scan_payload(compact_conn, compact_id)
    assert compact_row["raw_payload_format"] == SYMBOL_REFS_V1
    assert inline_row["raw_payload_format"] == INLINE_V1
    for key in (
        "total_valid_setups",
        "near_misses",
        "rejected",
        "data_issues",
        "actionable_a_grade_setups",
        "confirmed_setups",
        "candidate_a_grade_setups",
    ):
        assert compact_row[key] == inline_row[key]
    assert compact_children == inline_children
    assert json.loads(inline_row["raw_payload_json"]) == json.loads(_json_dump(oracle))
    assert _json_dump(loaded) == _json_dump(oracle)
    assert "research_provenance" in json.loads(compact_row["runtime_stats_json"])


def test_original_result_order_survives_reordered_supplied_payload(tmp_path: Path) -> None:
    result = _scan_result()
    oracle = _oracle_payload(result, cli=True)
    reordered = json.loads(_json_dump(oracle))
    reordered["results"] = list(reversed(reordered["results"]))
    run_id = _store(tmp_path / "order.sqlite", result, raw_payload=reordered)
    with open_read_only_database(tmp_path / "order.sqlite") as connection:
        physical = _physical(connection, run_id)
        loaded = load_logical_scan_payload(connection, run_id)
        children = tuple(
            row[0]
            for row in connection.execute(
                "SELECT symbol FROM symbol_results WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
        )
    assert physical["raw_payload_format"] == SYMBOL_REFS_V1
    assert [item["symbol"] for item in loaded["results"]] == ["XRPUSDT", "ETHUSDT", "BTCUSDT"]
    assert children == ("BTCUSDT", "ETHUSDT", "XRPUSDT")
    assert _json_dump(loaded) == _json_dump(reordered)


def test_unknown_unicode_null_and_unverified_fields_survive_compact_and_inline(tmp_path: Path) -> None:
    result = _scan_result()
    compact_oracle = _oracle_payload(result)
    compact_oracle["custom_top"] = "π-值"
    compact_oracle["absent_vs_null"] = None
    compact_id = _store(tmp_path / "top.sqlite", result, raw_payload=compact_oracle)
    inline_oracle = json.loads(_json_dump(compact_oracle))
    inline_oracle["results"][0]["nested_unknown"] = {
        "flag": True,
        "count": 1,
        "ratio": 1.0,
        "label": "1",
        "missing": None,
        "na": "N/A",
        "unverified": "Unverified",
    }
    inline_id = _store(tmp_path / "nested.sqlite", result, raw_payload=inline_oracle)
    with open_read_only_database(tmp_path / "top.sqlite") as connection:
        compact_row = _physical(connection, compact_id)
        loaded_compact = load_logical_scan_payload(connection, compact_id)
    with open_read_only_database(tmp_path / "nested.sqlite") as connection:
        inline_row = _physical(connection, inline_id)
        loaded_inline = load_logical_scan_payload(connection, inline_id)
    assert compact_row["raw_payload_format"] == SYMBOL_REFS_V1
    assert loaded_compact["custom_top"] == "π-值"
    assert loaded_compact["absent_vs_null"] is None
    assert _json_dump(loaded_compact) == _json_dump(compact_oracle)
    assert inline_row["raw_payload_format"] == INLINE_V1
    assert loaded_inline["results"][0]["nested_unknown"]["flag"] is True
    assert loaded_inline["results"][0]["nested_unknown"]["count"] == 1
    assert loaded_inline["results"][0]["nested_unknown"]["ratio"] == 1.0
    assert loaded_inline["results"][0]["nested_unknown"]["label"] == "1"
    assert "missing" in loaded_inline["results"][0]["nested_unknown"]
    assert loaded_inline["results"][0]["nested_unknown"]["missing"] is None
    assert _json_dump(loaded_inline) == _json_dump(inline_oracle)


def _drop_results(payload: dict) -> dict:
    payload.pop("results", None)
    return payload


def _empty_results(payload: dict) -> dict:
    payload["results"] = []
    return payload


def _non_list_results(payload: dict) -> dict:
    payload["results"] = {"symbol": "BTCUSDT"}
    return payload


def _subset_results(payload: dict) -> dict:
    payload["results"] = payload["results"][:1]
    return payload


def _changed_result_value(payload: dict) -> dict:
    payload["results"][0]["technical_score"] = 1
    return payload


def _duplicate_result_symbol(payload: dict) -> dict:
    payload["results"] = list(payload["results"]) + [dict(payload["results"][0])]
    return payload


@pytest.mark.parametrize(
    ("mutator", "expected_reason"),
    [
        (_drop_results, FALLBACK_RESULTS_MISSING),
        (_empty_results, FALLBACK_RESULTS_EMPTY),
        (_non_list_results, FALLBACK_RESULTS_NOT_LIST),
        (_subset_results, FALLBACK_SYMBOL_SET_MISMATCH),
        (_changed_result_value, FALLBACK_PAYLOAD_CHILD_BYTES_MISMATCH),
        (_duplicate_result_symbol, FALLBACK_DUPLICATE_SYMBOLS),
    ],
)
def test_divergent_or_malformed_supplied_payloads_stay_inline(tmp_path: Path, mutator, expected_reason) -> None:
    result = _scan_result()
    payload = json.loads(_json_dump(_oracle_payload(result, cli=True)))
    mutated = mutator(payload)
    ranked = {item.symbol_result.symbol: item for item in rank_scan_results(result.results)}
    records = tuple(
        repositories_module._symbol_result_record(
            run_id="probe",
            result=result,
            symbol_result=symbol_result,
            ranked=ranked.get(symbol_result.symbol),
            portfolio_decision="N/A",
        )
        for symbol_result in result.results
    )
    encoded = encode_scan_raw_payload(mutated, records, dumps=_json_dump)
    assert encoded.format == INLINE_V1
    assert encoded.reason == expected_reason
    run_id = _store(tmp_path / f"{expected_reason}.sqlite", result, raw_payload=mutated)
    with open_read_only_database(tmp_path / f"{expected_reason}.sqlite") as connection:
        physical = _physical(connection, run_id)
        loaded = load_logical_scan_payload(connection, run_id)
    assert physical["raw_payload_format"] == INLINE_V1
    assert _json_dump(loaded) == _json_dump(mutated)


def test_reference_looking_user_fields_are_not_inferred_as_compact(tmp_path: Path) -> None:
    result = _scan_result()
    payload = json.loads(_json_dump(_oracle_payload(result)))
    payload["results"] = [{"symbol": "BTCUSDT", "sha256": "0" * 64}]
    run_id = _store(tmp_path / "looks-like-refs.sqlite", result, raw_payload=payload)
    with open_read_only_database(tmp_path / "looks-like-refs.sqlite") as connection:
        physical = _physical(connection, run_id)
        loaded = load_logical_scan_payload(connection, run_id)
    assert physical["raw_payload_format"] == INLINE_V1
    assert loaded["results"] == payload["results"]


def test_inline_write_rollback_reads_existing_reference_rows(tmp_path: Path) -> None:
    result = _scan_result()
    db_path = tmp_path / "mixed.sqlite"
    ref_id = _store(db_path, result, run_id="ref-run")
    inline_id = _store(db_path, result, inline=True, run_id="inline-run")
    with open_read_only_database(db_path) as connection:
        assert _physical(connection, ref_id)["raw_payload_format"] == SYMBOL_REFS_V1
        assert _physical(connection, inline_id)["raw_payload_format"] == INLINE_V1
        assert _json_dump(load_logical_scan_payload(connection, ref_id)) == _json_dump(_oracle_payload(result))
        assert _json_dump(load_logical_scan_payload(connection, inline_id)) == _json_dump(_oracle_payload(result))
    with open_read_only_database(db_path) as connection:
        assert _json_dump(load_logical_scan_payload(connection, ref_id)) == _json_dump(_oracle_payload(result))


def test_v24_fixture_reads_without_migration_and_unknown_format_fails(tmp_path: Path) -> None:
    result = _scan_result()
    oracle = _oracle_payload(result)
    children = {
        item["symbol"]: _json_dump(item)
        for item in oracle["results"]
    }
    v24_path = tmp_path / "legacy-v24.sqlite"
    _write_v24_fixture(v24_path, run_id="legacy", payload=oracle, children=children)
    with sqlite3.connect(v24_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scan_runs)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        loaded = load_logical_scan_payload(connection, "legacy")
    assert "raw_payload_format" not in columns
    assert version == 24
    assert _json_dump(loaded) == _json_dump(oracle)

    db_path = tmp_path / "future.sqlite"
    run_id = _store(db_path, result)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE scan_runs SET raw_payload_format = 'future_v9' WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError, match="unsupported"):
            load_logical_scan_payload(connection, run_id)


def test_v24_to_v25_migration_is_additive_and_preserves_payload_bytes(tmp_path: Path) -> None:
    result = _scan_result()
    oracle = _oracle_payload(result)
    children = {item["symbol"]: _json_dump(item) for item in oracle["results"]}
    db_path = tmp_path / "migrate.sqlite"
    _write_v24_fixture(db_path, run_id="legacy", payload=oracle, children=children)
    with sqlite3.connect(db_path) as connection:
        before_payload = connection.execute(
            "SELECT raw_payload_json FROM scan_runs WHERE run_id = 'legacy'"
        ).fetchone()[0]
        before_children = _child_map(connection, "legacy")
        before_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert before_version == 24
    with open_initialized_database(db_path):
        pass
    with open_initialized_database(db_path):
        pass
    with sqlite3.connect(db_path) as connection:
        after_payload = connection.execute(
            "SELECT raw_payload_json, raw_payload_format FROM scan_runs WHERE run_id = 'legacy'"
        ).fetchone()
        after_children = _child_map(connection, "legacy")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        loaded = load_logical_scan_payload(connection, "legacy")
    assert version == SCHEMA_VERSION == 25
    assert after_payload[0] == before_payload
    assert after_payload[1] == INLINE_V1
    assert after_children == before_children
    assert _json_dump(loaded) == _json_dump(oracle)


def test_injected_v25_migration_failure_rolls_back_and_retries(tmp_path: Path, monkeypatch) -> None:
    result = _scan_result()
    oracle = _oracle_payload(result)
    children = {item["symbol"]: _json_dump(item) for item in oracle["results"]}
    db_path = tmp_path / "fail-migrate.sqlite"
    _write_v24_fixture(db_path, run_id="legacy", payload=oracle, children=children)

    def boom(connection: sqlite3.Connection) -> None:
        del connection
        raise sqlite3.Error("injected migration failure")

    monkeypatch.setattr(database_module, "_migrate_scan_raw_payload_format_v25", boom)
    with pytest.raises(StorageError):
        open_initialized_database(db_path)
    with sqlite3.connect(db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scan_runs)")}
        payload = connection.execute("SELECT raw_payload_json FROM scan_runs WHERE run_id = 'legacy'").fetchone()[0]
    assert version == 24
    assert "raw_payload_format" not in columns
    assert payload == _json_dump(oracle)
    monkeypatch.undo()
    with open_initialized_database(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
        assert _physical(connection, "legacy")["raw_payload_format"] == INLINE_V1


def test_integrity_failures_do_not_partially_reconstruct(tmp_path: Path) -> None:
    result = _scan_result()

    hash_path = tmp_path / "hash.sqlite"
    hash_id = _store(hash_path, result)
    with sqlite3.connect(hash_path) as connection:
        children = _child_map(connection, hash_id)
        connection.execute(
            "UPDATE symbol_results SET raw_result_json = ? WHERE run_id = ? AND symbol = 'BTCUSDT'",
            (children["ETHUSDT"], hash_id),
        )
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError, match="hash mismatch"):
            load_logical_scan_payload(connection, hash_id)

    missing_path = tmp_path / "missing.sqlite"
    missing_id = _store(missing_path, result)
    with sqlite3.connect(missing_path) as connection:
        connection.execute("DELETE FROM symbol_results WHERE run_id = ? AND symbol = 'ETHUSDT'", (missing_id,))
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError, match="membership"):
            load_logical_scan_payload(connection, missing_id)

    extra_path = tmp_path / "extra.sqlite"
    extra_id = _store(extra_path, result)
    with sqlite3.connect(extra_path) as connection:
        connection.execute(
            """
            INSERT INTO symbol_results (
                run_id, symbol, status, display_bucket, readiness_score, setup_quality_score,
                edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
                regime_state, pullback_status, portfolio_decision, derivatives_context_json,
                volume_profile_context_json, raw_result_json
            )
            SELECT run_id, 'DOGEUSDT', status, display_bucket, readiness_score, setup_quality_score,
                   edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
                   regime_state, pullback_status, portfolio_decision, derivatives_context_json,
                   volume_profile_context_json, raw_result_json
            FROM symbol_results
            WHERE run_id = ? AND symbol = 'BTCUSDT'
            """,
            (extra_id,),
        )
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError, match="membership"):
            load_logical_scan_payload(connection, extra_id)

    dup_path = tmp_path / "dup-ref.sqlite"
    dup_id = _store(dup_path, result)
    with sqlite3.connect(dup_path) as connection:
        compact = json.loads(
            connection.execute("SELECT raw_payload_json FROM scan_runs WHERE run_id = ?", (dup_id,)).fetchone()[0]
        )
        compact["results"].append(dict(compact["results"][0]))
        connection.execute(
            "UPDATE scan_runs SET raw_payload_json = ? WHERE run_id = ?",
            (_json_dump(compact), dup_id),
        )
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError, match="duplicate"):
            load_logical_scan_payload(connection, dup_id)


def test_missing_format_value_and_malformed_compact_payload_fail(tmp_path: Path) -> None:
    result = _scan_result()
    db_path = tmp_path / "malformed.sqlite"
    run_id = _store(db_path, result)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE scan_runs SET raw_payload_json = '{}' WHERE run_id = ?", (run_id,))
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError):
            load_logical_scan_payload(connection, run_id)
        connection.execute(
            "UPDATE scan_runs SET raw_payload_json = ? WHERE run_id = ?",
            (_json_dump({"results": "nope"}), run_id),
        )
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError):
            load_logical_scan_payload(connection, run_id)
        connection.execute("UPDATE scan_runs SET raw_payload_format = '' WHERE run_id = ?", (run_id,))
        connection.execute(
            "UPDATE scan_runs SET raw_payload_json = ? WHERE run_id = ?",
            (_json_dump(_oracle_payload(result)), run_id),
        )
        connection.commit()
        with pytest.raises(ScanPayloadIntegrityError, match="raw_payload_format is required"):
            load_logical_scan_payload(connection, run_id)


def test_child_insert_failure_leaves_no_partial_scan(tmp_path: Path, monkeypatch) -> None:
    def boom(connection: sqlite3.Connection, records) -> None:
        del connection, records
        raise sqlite3.IntegrityError("injected child failure")

    monkeypatch.setattr(repositories_module, "_insert_symbol_results", boom)
    db_path = tmp_path / "partial.sqlite"
    with pytest.raises(StorageError):
        _store(db_path, _scan_result())
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM symbol_results").fetchone()[0] == 0


def test_duplicate_run_id_still_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "dup.sqlite"
    _store(db_path, _scan_result(), run_id="fixed")
    with pytest.raises(StorageError):
        _store(db_path, _scan_result(), run_id="fixed")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM symbol_results").fetchone()[0] == 3


def test_decoder_uses_one_child_query_and_does_not_cross_bind_runs(tmp_path: Path) -> None:
    result = _scan_result()
    db_path = tmp_path / "fanout.sqlite"
    first = _store(db_path, result, run_id="run-a")
    second_result = result.model_copy(update={"results": (_near_miss_symbol(), _rejected_symbol(), _valid_symbol())})
    second = _store(db_path, second_result, run_id="run-b")
    with open_read_only_database(db_path) as connection:
        queries: list[str] = []
        original = connection.execute

        def wrapped(sql, parameters=()):
            text = str(sql)
            if "symbol_results" in text.lower() and "select" in text.lower():
                queries.append(text)
            return original(sql, parameters)

        connection.execute = wrapped  # type: ignore[method-assign]
        loaded = load_logical_scan_payload(connection, first)
    assert len(queries) == 1
    assert "run_id" in queries[0]
    assert [item["symbol"] for item in loaded["results"]] == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    with open_read_only_database(db_path) as connection:
        loaded_b = load_logical_scan_payload(connection, second)
    assert [item["symbol"] for item in loaded_b["results"]] == ["ETHUSDT", "XRPUSDT", "BTCUSDT"]
    assert loaded["results"][0]["symbol"] == "BTCUSDT"


def test_wolf_briefing_and_research_and_history_export_use_logical_results(tmp_path: Path) -> None:
    result = _scan_result()
    db_path = tmp_path / "consumers.sqlite"
    _store(db_path, result)
    artifacts = load_latest_db_scan_artifacts(project_root=tmp_path, database_path=db_path)
    assert artifacts.scan_payload is not None
    assert artifacts.scan_payload["results"][0]["symbol"] == "BTCUSDT"
    assert "sha256" not in artifacts.scan_payload["results"][0]
    assert "display_bucket" in artifacts.scan_payload["results"][0]
    report = build_research_report(db_path, query="summary")
    assert report["summary"]["total_scan_runs"] == 1
    assert report["summary"]["total_symbols_scanned"] == 3
    assert report["summary"]["total_valid_setups"] == 1
    history = export_history_payload(db_path, limit=1)
    assert history[0]["valid_setups"] == 1
    assert "results" not in history[0]


def test_standalone_resume_json_is_unchanged(tmp_path: Path) -> None:
    result = _scan_result()
    payload = run_scan._json_payload(result, ranked_results=rank_scan_results(result.results))
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    resume = run_scan._load_resume_state(path, ("BTCUSDT", "ETHUSDT", "XRPUSDT"), skip_completed=False)
    assert set(resume.loaded_symbols) == {"BTCUSDT", "ETHUSDT", "XRPUSDT"}
    assert resume.results_by_symbol["BTCUSDT"].symbol == "BTCUSDT"


def test_inline_only_setting_reason_is_classified() -> None:
    result = _scan_result()
    ranked = {item.symbol_result.symbol: item for item in rank_scan_results(result.results)}
    records = tuple(
        repositories_module._symbol_result_record(
            run_id="probe",
            result=result,
            symbol_result=symbol_result,
            ranked=ranked.get(symbol_result.symbol),
            portfolio_decision="N/A",
        )
        for symbol_result in result.results
    )
    encoded = encode_scan_raw_payload(_oracle_payload(result), records, dumps=_json_dump, inline_only=True)
    assert encoded.format == INLINE_V1
    assert encoded.reason == FALLBACK_INLINE_REQUESTED
