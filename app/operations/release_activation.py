"""Exact, bounded feature activation. This module never opens a database."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import PureWindowsPath
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


FEATURE_KEYS = frozenset({"TELEGRAM_SIGNAL_PRESENTATION", "CMC_MODE", "CMC_SIDECAR_PATH",
    "CMC_IDENTITY_MANIFEST", "CMC_COLLECTION_PROFILE", "CMC_COLLECTION_PROFILE_PATH",
    "CMC_DAILY_CREDITS", "CMC_MONTHLY_CREDITS"})
ARTIFACT_KEYS = frozenset({"CMC_IDENTITY_MANIFEST", "CMC_COLLECTION_PROFILE_PATH"})


def immutable_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def activate(overrides, packet, *, contract, role, release, tree, source_hash, read_bytes):
    expected_keys = {"role", "release_sha", "candidate_tree", "expected_identity",
        "source_profile_sha256", "contract_sha256", "changes", "artifacts"}
    if not isinstance(packet, dict) or set(packet) != expected_keys:
        raise ValueError("Unknown or missing activation packet keys")
    review = contract.get("activation_reviews", {}).get(role)
    # The reviewed digest is carried outside the contract digest to avoid a cycle.
    contract_body = {k:v for k,v in contract.items() if k != "activation_reviews"}
    if (review != digest(packet) or packet["contract_sha256"] != digest(contract_body)
            or packet["role"] != role or packet["release_sha"] != release
            or packet["candidate_tree"] != tree or packet["source_profile_sha256"] != source_hash
            or packet["expected_identity"] != contract["expected_identity"]
            or contract.get("release_trees", {}).get(release) != tree
            or not immutable_sha(release) or not immutable_sha(tree)):
        raise ValueError("Activation review, exact release/tree, profile or epoch binding differs")
    changes = packet["changes"]
    if not isinstance(changes, dict) or not changes or not set(changes) <= FEATURE_KEYS:
        raise ValueError("Unknown or empty feature changes")
    result = dict(overrides)
    for key, change in changes.items():
        if (not isinstance(change, dict) or set(change) != {"before", "after"}
                or change["before"] != overrides.get(key) or change["before"] == change["after"]
                or not isinstance(change["after"], str)):
            raise ValueError("Exact setting differences required")
        result[key] = change["after"]
    if result.get("TELEGRAM_SIGNAL_PRESENTATION", "legacy") not in {"legacy", "precision"}:
        raise ValueError("Unsupported Telegram presentation")
    if result.get("CMC_MODE") not in {"off", "shadow"}:
        raise ValueError("Only CMC off or observational shadow is reviewed")
    artifacts = packet["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) - ARTIFACT_KEYS:
        raise ValueError("Unknown activation artifacts")
    if result["CMC_MODE"] == "shadow":
        required = FEATURE_KEYS - {"TELEGRAM_SIGNAL_PRESENTATION"}
        if not required <= result.keys() or result["CMC_COLLECTION_PROFILE"] != "CUSTOM":
            raise ValueError("Shadow requires exact sidecar, identity, custom profile and budget")
        for key, maximum in (("CMC_DAILY_CREDITS", 388), ("CMC_MONTHLY_CREDITS", 11988)):
            if not result[key].isdigit() or not 1 <= int(result[key]) <= maximum:
                raise ValueError("Unapproved CMC credit limit")
        root = PureWindowsPath(contract["state_root"])
        for key in ("CMC_SIDECAR_PATH", *sorted(ARTIFACT_KEYS)):
            path = PureWindowsPath(result[key])
            relative_sidecar = (key == "CMC_SIDECAR_PATH" and not path.is_absolute()
                and len(path.parts) == 2 and path.parts[0] == "research_runs")
            if ((not relative_sidecar and (not path.is_absolute() or root not in path.parents)) or ".." in path.parts
                    or ":" in str(path)[2:] or path.suffix.lower() not in
                    ({".db", ".sqlite"} if key == "CMC_SIDECAR_PATH" else {".json"})):
                raise ValueError("Feature path outside reviewed state root")
        if not PureWindowsPath(result["CMC_SIDECAR_PATH"]).name.startswith("cmc_"):
            raise ValueError("Dedicated CMC sidecar required")
        if len({result[k].casefold() for k in ("CMC_SIDECAR_PATH", *ARTIFACT_KEYS)}) != 3:
            raise ValueError("Feature paths collide")
        if set(artifacts) != ARTIFACT_KEYS:
            raise ValueError("Exact identity and collection artifact hashes required")
    elif artifacts:
        raise ValueError("CMC-off packet must not change evidence artifacts")
    for key, artifact in artifacts.items():
        if (not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}
                or artifact["path"] != result.get(key)):
            raise ValueError("Changed artifact path")
        body = read_bytes(artifact["path"])
        maximum = 256 * 1024 if key == "CMC_IDENTITY_MANIFEST" else 32768
        if len(body) > maximum or hashlib.sha256(body).hexdigest() != artifact["sha256"]:
            raise ValueError("Artifact content differs from reviewed hash")
        if key == "CMC_COLLECTION_PROFILE_PATH":
            from app.cmc.profiles import CollectionProfile
            profile = CollectionProfile.model_validate_json(body)
            if profile.name != "CUSTOM":
                raise ValueError("Exact custom collection profile required")
        else:
            from app.cmc.collection import IdentityManifest
            IdentityManifest.model_validate_json(body)
    return result


def validate_approval(approval, *, profile, role, contract, now=None):
    expected = {"schema", "role", "release_sha", "candidate_tree", "expected_identity",
        "profile_sha256", "contract_sha256", "activation_sha256", "changes",
        "issued_at", "expires_at", "owner_approval_ref"}
    if set(approval) != expected or approval["schema"] != "cci-feature-approval/1":
        raise ValueError("Separate exact-settings owner approval required")
    from app.cmc.models import utc
    issued, expires = utc(approval["issued_at"]), utc(approval["expires_at"])
    now = now or datetime.now(UTC)
    if (issued is None or expires is None or not issued <= now < expires
            or expires - issued > timedelta(hours=24)):
        raise ValueError("Owner approval is stale or outside bounded validity")
    packet = profile.get("_activation")
    reference = approval["owner_approval_ref"]
    if (not isinstance(reference, str) or not reference.strip() or reference.upper() in {"N/A", "PENDING", "UNVERIFIED"}
            or approval["role"] != role or approval["release_sha"] != profile["release_sha"]
            or approval["candidate_tree"] != profile["candidate_tree"]
            or approval["expected_identity"] != profile["expected_identity"]
            or approval["profile_sha256"] != digest(profile)
            or approval["contract_sha256"] != digest(contract)
            or approval["activation_sha256"] != digest(packet)
            or approval["changes"] != (packet["changes"] if packet else {})
            or contract.get("release_trees", {}).get(profile["release_sha"]) != profile["candidate_tree"]):
        raise ValueError("Owner approval differs from exact reviewed proposal/settings")
