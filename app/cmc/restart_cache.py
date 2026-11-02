"""Fragmented immutable pages with a last-written commit manifest.

All writes use the existing record, file, record-count and retention caps. A
crash, retention eviction or partial write makes a family a cache miss. Cache
recovery never acquires evidence or spends provider credits.
"""
import hashlib
import json
from decimal import Decimal

from app.cmc.client import Fetch
from app.cmc.models import stable_id, utc

KIND = "collection_family_v2"
MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_FAMILY_BYTES = 32 * 1024 * 1024


def restore_fetch(value):
    """Recover provider numeric types from original bytes, never rounded JSON.

    Pydantic's JSON representation converts Decimal in Any fields to text. The
    original bounded raw response is authoritative for those fields on restart.
    No receipt is reconciled and no acquisition timestamp is changed here.
    """
    fetch = Fetch.model_validate_json(value) if isinstance(value,str) else Fetch.model_validate(value)
    if fetch.raw_text is not None:
        raw = fetch.raw_text.encode("utf-8")
        if len(raw) > 2*1024*1024 or hashlib.sha256(raw).hexdigest() != fetch.raw_ref:
            raise ValueError("restart original response hash/budget differs")
        payload = json.loads(raw,parse_float=Decimal)
        if not isinstance(payload,dict) or "data" not in payload:
            raise ValueError("restart original response envelope differs")
        fetch = fetch.model_copy(update={"raw":payload,"data":payload["data"]})
    return fetch


def save_family(store, request_key, fetches):
    if not 1 <= len(fetches) <= 4 or any(f.error for f in fetches):
        raise ValueError("bounded successful family required")
    chunk_chars = min(256 * 1024, min(2 * 1024 * 1024, store.max_bytes // 4) // 4)
    pages, total = [], 0
    for fetch in fetches:
        text = fetch.model_dump_json()
        size = len(text.encode("utf-8"))
        total += size
        if size > MAX_PAGE_BYTES or total > MAX_FAMILY_BYTES:
            raise ValueError("restart cache serialized page budget")
        refs = []
        for index, start in enumerate(range(0, len(text), chunk_chars)):
            if index >= 128:
                raise ValueError("restart cache fragment budget")
            chunk = {"index":index, "text":text[start:start+chunk_chars]}
            identity = stable_id(("collection_page_fragment", chunk))
            refs.append(store.put("collection_page_fragment", chunk,
                acquired_at=fetch.acquired_at, identity=identity))
        pages.append({"fragments":refs, "bytes":size,
            "sha256":hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "acquired_at":fetch.acquired_at.isoformat()})
    manifest = {"version":2, "request_key":request_key, "pages":pages, "bytes":total}
    # Publishing last is intentional. Missing fragments remain unusable after restart.
    store.put(KIND, manifest, acquired_at=max(f.acquired_at for f in fetches))


def restore_family(store, manifest, as_of):
    if (set(manifest) != {"version", "request_key", "pages", "bytes"}
            or manifest["version"] != 2 or not 1 <= len(manifest["pages"]) <= 4
            or not 0 < manifest["bytes"] <= MAX_FAMILY_BYTES):
        raise ValueError("invalid restart manifest")
    result, total, seen = [], 0, set()
    for page in manifest["pages"]:
        refs = page["fragments"]
        acquired = utc(page["acquired_at"])
        if (not 1 <= len(refs) <= 128 or not 0 < page["bytes"] <= MAX_PAGE_BYTES
                or acquired is None or acquired > as_of or any(r in seen for r in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError("invalid restart page")
        seen.update(refs)
        parts = store.records_by_id(refs, as_of)
        if len(parts) != len(refs):
            raise ValueError("incomplete restart family")
        texts, text_bytes = [], 0
        for index, ref in enumerate(refs):
            chunk = parts[ref]
            if (chunk.get("index") != index or not isinstance(chunk.get("text"), str)
                    or stable_id(("collection_page_fragment", chunk)) != ref):
                raise ValueError("corrupt restart fragment")
            text_bytes += len(chunk["text"].encode("utf-8"))
            if text_bytes > page["bytes"]:
                raise ValueError("restart page read budget")
            texts.append(chunk["text"])
        text = "".join(texts)
        size = len(text.encode("utf-8"))
        total += size
        if size != page["bytes"] or hashlib.sha256(text.encode("utf-8")).hexdigest() != page["sha256"]:
            raise ValueError("corrupt restart page hash")
        fetch = restore_fetch(text)
        if fetch.acquired_at != acquired or fetch.error:
            raise ValueError("changed original acquisition")
        result.append(fetch)
    if total != manifest["bytes"] or len({f.endpoint for f in result}) != 1:
        raise ValueError("inconsistent restart family")
    return tuple(result)
