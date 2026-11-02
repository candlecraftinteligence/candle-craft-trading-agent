"""Retained rich reservations are never downgraded, rewritten or blindly replayed."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import types

import httpx
import pytest

from app.alerts.telegram_rich_message import pack_rich_message
from app.operations.rollback_compatibility import assess_retained_intents, require_rollback_proof
from app.alerts.telegram_outbox_protocol import TelegramOutboxWorker, deliver_single_reserved_part
from app.storage.database import connect_database
from tests.test_p0b3b1_telegram_outbox_protocol import _seed_canonical_event, FakeTelegramTransport, NOW
from tests.runtime_epoch_support import bootstrap_operational_test_database, SYNTHETIC_IDENTITY

pytestmark = pytest.mark.no_auto_epoch
ROOT = Path(__file__).resolve().parents[1]


def retained(state="PENDING",text=None):
    text = text or pack_rich_message("<b>SYNTHETIC</b>")
    return {"delivery_state":state,"payload_text":text,"payload_hash":hashlib.sha256(text.encode()).hexdigest()}


@pytest.mark.parametrize("state", ["PENDING", "RETRYABLE", "IN_FLIGHT", "UNCERTAIN", "SENT"])
def test_retained_states_require_exact_transport_capability(state):
    records = [retained(state),retained(state,"legacy synthetic")]
    before = copy.deepcopy(records)
    assert assess_retained_intents(records,target_formats={"plain_text"},inventory_complete=True)["status"] == "FAIL"
    assert assess_retained_intents(records,target_formats={"plain_text","CCI_RICH_MESSAGE_V1"},inventory_complete=True)["status"] == "PASS"
    assert assess_retained_intents(records,target_formats={"plain_text","CCI_RICH_MESSAGE_V1"})["status"] == "FAIL"
    assert records == before


@pytest.mark.parametrize("commit,method", [("1d163143ee99b14a1c5c7576ebddc1843c9b945f","sendMessage"),
    ("0086e9e508e9b4b75f1a6c0c0c8210be5260c326","sendRichMessage")])
def test_exact_previous_and_rich_compatible_transport_git_objects(commit,method):
    source = subprocess.check_output(["git","show",f"{commit}:app/alerts/telegram.py"],cwd=ROOT).decode()
    module = types.ModuleType("synthetic_exact_transport")
    exec(compile(source,f"{commit}:app/alerts/telegram.py","exec"),module.__dict__)
    calls = []
    payload = pack_rich_message("<b>OFFLINE SYNTHETIC ONLY</b>")
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={"ok":True,"result":{"message_id":1,"chat":{"id":1}}})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url="https://synthetic.invalid") as http:
            await module.send_telegram_message_part(bot_token="SYNTHETIC",chat_id="SYNTHETIC",message=payload,http_client=http)
    asyncio.run(exercise())
    assert len(calls) == 1 and calls[0].url.path.endswith("/"+method)
    body = json.loads(calls[0].content)
    assert (body["text"] == payload) if method == "sendMessage" else (body["rich_message"]["html"] == "<b>OFFLINE SYNTHETIC ONLY</b>")


@pytest.mark.parametrize("state", ["PENDING", "RETRYABLE", "IN_FLIGHT", "UNCERTAIN"])
def test_synthetic_rich_outbox_state_restart_preserves_payload_and_no_unknown_replay(tmp_path,state):
    path = bootstrap_operational_test_database(tmp_path/"synthetic-rich.db")
    payload = pack_rich_message("<b>SYNTHETIC RESERVED</b>")
    event, attempt = _seed_canonical_event(path,message=payload,initial_event_state=state,initial_part_state=state)
    offset = tmp_path/"listener-state.json"
    offset.write_text(json.dumps({"latest_processed_update_id":700,"synthetic":True}))
    original_offset = offset.read_bytes()
    def rows():
        with connect_database(path) as db:
            return [tuple(r) for r in db.execute("SELECT payload_text,payload_hash FROM public_alert_delivery_parts WHERE public_alert_event_id=?",(event,))]
    before = rows()
    async def exercise():
        worker = TelegramOutboxWorker()
        transport = FakeTelegramTransport()
        try:
            result = await deliver_single_reserved_part(worker=worker,database_path=path,event_id=event,
                reservation_id=attempt,expected_identity=SYNTHETIC_IDENTITY,now=NOW,transport=transport)
            if state in {"IN_FLIGHT","UNCERTAIN"}:
                assert result.status in {"blocked", "skipped"} and transport.calls == []
                assert result.transport_outcome is None
            else:
                assert result.status == "sent" and len(transport.calls) == 1
                assert transport.calls[0].payload_text == payload
                again = await deliver_single_reserved_part(worker=worker,database_path=path,event_id=event,
                    reservation_id=attempt,expected_identity=SYNTHETIC_IDENTITY,now=NOW,transport=transport)
                assert again.status == "blocked" and len(transport.calls) == 1
        finally:
            await worker.aclose()
    asyncio.run(exercise())
    assert rows() == before and offset.read_bytes() == original_offset


def test_historical_rollback_contract_is_not_current_rich_compatibility_proof():
    contract = json.loads((ROOT/"config/releases/runtime-profile-contract.json").read_text())
    with pytest.raises(ValueError,match="rich-compatible"):
        require_rollback_proof(contract,contract["rollback"],"d"*40)
