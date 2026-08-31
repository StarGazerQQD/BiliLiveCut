"""Stable, release-independent identities for Portable ASR model content."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

IDENTITY_SCHEMA_VERSION = 1


class _SubModel(Protocol):
    """Minimal sub-model definition accepted by the identity builder."""

    hub: str
    repository: str
    resolved_revision: str
    target_subdir: str


class _Component(Protocol):
    """Minimal third-party component definition accepted by the identity builder."""

    repository: str
    revision: str
    target_subdir: str


class _CatalogEngine(Protocol):
    """Minimal catalog engine definition accepted by the identity builder."""

    engine_id: str
    hub: str
    repository: str
    resolved_revision: str
    target_path: str
    sub_models: list[_SubModel]
    third_party_components: list[_Component]


class _ManifestEngine(Protocol):
    """Minimal internal-manifest engine definition used for comparison."""

    engine_id: str
    hub: str
    model_id: str
    model_repo: str | None
    revision: str | None
    target_path: str
    sub_models: list[dict[str, object]]
    third_party_components: list[dict[str, object]]


def _canonical_sha256(payload: object) -> str:
    """Hash one JSON-compatible value using a stable canonical encoding."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def catalog_engine_identity(engine: _CatalogEngine) -> dict[str, object]:
    """Build content identity from one immutable model-catalog entry.

    Release versions, filenames, timestamps, generated metadata and source
    commits are intentionally absent.  A repository plus its resolved revision
    identifies the upstream snapshot; nested models and bundled components are
    part of the same identity.
    """
    return {
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "engine_id": engine.engine_id,
        "hub": engine.hub,
        "repository": engine.repository,
        "resolved_revision": engine.resolved_revision,
        "target_path": engine.target_path,
        "sub_models": [
            {
                "hub": sub.hub,
                "repository": sub.repository,
                "resolved_revision": sub.resolved_revision,
                "target_subdir": sub.target_subdir,
            }
            for sub in sorted(engine.sub_models, key=lambda item: item.target_subdir)
        ],
        "third_party_components": [
            {
                "repository": component.repository,
                "revision": component.revision,
                "target_subdir": component.target_subdir,
            }
            for component in sorted(engine.third_party_components, key=lambda item: item.target_subdir)
        ],
    }


def manifest_engine_identity(engine: _ManifestEngine) -> dict[str, object]:
    """Build the same identity from an Engine Pack manifest definition."""
    repository = engine.model_repo or engine.model_id
    return {
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "engine_id": engine.engine_id,
        "hub": engine.hub,
        "repository": repository,
        "resolved_revision": engine.revision or "",
        "target_path": engine.target_path,
        "sub_models": [
            {
                "hub": str(item["hub"]),
                "repository": str(item["model_id"]),
                "resolved_revision": str(item["revision"] or ""),
                "target_subdir": str(item["target_subdir"]),
            }
            for item in sorted(engine.sub_models, key=lambda value: str(value["target_subdir"]))
        ],
        "third_party_components": [
            {
                "repository": str(item["repository"]),
                "revision": str(item["revision"]),
                "target_subdir": str(item["target_subdir"]),
            }
            for item in sorted(engine.third_party_components, key=lambda value: str(value["target_subdir"]))
        ],
    }


def engine_fingerprint(identity: Mapping[str, object]) -> str:
    """Return the stable SHA-256 fingerprint of one engine identity."""
    return _canonical_sha256(dict(identity))


def desired_engine_records(engines: Iterable[_CatalogEngine]) -> dict[str, dict[str, object]]:
    """Return identity and fingerprint records keyed by engine ID."""
    result: dict[str, dict[str, object]] = {}
    for engine in engines:
        identity = catalog_engine_identity(engine)
        result[engine.engine_id] = {
            "identity": identity,
            "content_fingerprint": engine_fingerprint(identity),
        }
    return result


def model_set_fingerprint(engine_records: Mapping[str, Mapping[str, object]]) -> str:
    """Hash the ordered engine-ID to content-fingerprint mapping."""
    payload: dict[str, Any] = {
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "engines": {
            engine_id: str(record["content_fingerprint"]) for engine_id, record in sorted(engine_records.items())
        },
    }
    return _canonical_sha256(payload)
