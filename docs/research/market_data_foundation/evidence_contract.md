# Evidence contract

An observation carries run id, code SHA, policy digest, provider, endpoint, sanitized parameters, venue and instrument ids, native and normalized value and unit, timestamps, interval semantics, cache status, lineage, response status, latency, retries, request weight when a header actually provides it, raw hash when the body may be kept locally, and a disposition.

Dispositions: verified, missing, stale, malformed, incompatible, unverified, unsupported, access_blocked, not_applicable.

CCI marks: missing, stale, unsupported, and not_applicable display as N/A. Unverified, malformed, incompatible, and access_blocked display as Unverified. Missing is never the number zero.

Separate dimensions, which a single HTTP 200 does not satisfy: transport success, field presence, semantic validity, identity compatibility, temporal coverage, original-decision availability, and sufficiency for a named consumer.

Publication latency is reported only when a real publication timestamp exists. Otherwise the limitation is named.
