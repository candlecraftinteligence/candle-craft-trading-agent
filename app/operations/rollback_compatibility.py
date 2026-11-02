"""Pure retained-intent assessment. No database access, mutation or replay."""
import hashlib

from app.alerts.telegram_rich_message import is_rich_message, rich_message_html
from app.operations.release_activation import digest, immutable_sha

STATES = {"PENDING", "RETRYABLE", "IN_FLIGHT", "UNCERTAIN", "SENT", "FAILED_FINAL", "SKIPPED_DRY_RUN", "POLICY_DISABLED"}


def assess_retained_intents(records, *, target_formats, inventory_complete=False):
    if len(records) > 20000:
        raise ValueError("bounded retained-intent inventory required")
    required, reasons, counts = set(), [], {}
    if not inventory_complete:
        reasons.append("complete_retained_outbox_inventory_required")
    for row in records:
        state = row.get("delivery_state")
        counts[state] = counts.get(state, 0)+1
        if state not in STATES:
            reasons.append("unknown_outbox_state")
        text = row.get("payload_text")
        if not isinstance(text,str) or not text:
            reasons.append("missing_reserved_payload")
            continue
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != row.get("payload_hash"):
            reasons.append("reserved_payload_hash_mismatch")
        if is_rich_message(text):
            try: rich_message_html(text)
            except ValueError: reasons.append("invalid_rich_reservation")
            required.add("CCI_RICH_MESSAGE_V1")
        elif text.startswith("CCI_RICH_MESSAGE_"):
            reasons.append("unknown_reserved_format")
        else:
            required.add("plain_text")
    if required - set(target_formats):
        reasons.append("target_cannot_transmit_retained_format")
    return {"status":"FAIL" if reasons else "PASS", "required_formats":sorted(required),
        "states":counts,"withholding_reasons":sorted(set(reasons)),"inventory_sha256":digest(records),
        "in_flight_or_uncertain_require_no_replay":bool(counts.get("IN_FLIGHT") or counts.get("UNCERTAIN")),
        "mutated_or_replayed":False}


def require_rollback_proof(contract, release, tree):
    proof = contract.get("rollback_compatibility")
    if (not isinstance(proof,dict) or proof.get("release_sha") != release
            or proof.get("candidate_tree") != tree or not immutable_sha(release) or not immutable_sha(tree)
            or proof.get("status") != "PASS" or not proof.get("owner_approval_ref")
            or proof.get("inventory_complete") is not True
            or not {"plain_text", "CCI_RICH_MESSAGE_V1"} <= set(proof.get("tested_formats", []))
            or not {"sendMessage", "sendRichMessage"} <= set(proof.get("tested_routes", []))
            or any(proof.get(k) is not True for k in ("reservations_unchanged", "canonical_dedup_preserved",
                "telegram_offset_preserved", "uncertain_never_replayed", "current_inventory_matches"))
            or any(not isinstance(proof.get(k),str) or len(proof[k]) != 64 for k in
                ("inventory_sha256", "roundtrip_receipt_sha256"))):
        raise ValueError("Code rollback requires a new exact rich-compatible transport/state proof; otherwise controlled stop")
    return proof
