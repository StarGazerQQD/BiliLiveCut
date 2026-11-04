from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.analysis import llm, llm_providers
from app.analysis.event_enricher import commit_event_enrichment, compute_event_enrichment, enrich_hotspot_event
from app.core.configuration import ConfigurationChange, save_configuration
from app.core.runtime_settings import settings_scope
from app.db.entities import AppSetting, HotspotEvent
from app.db.session import get_session
from app.models.authorization_revision import AUTHORIZATION_REVISIONS_KEY, authorization_revisions
from app.models.configuration import model_configuration_view, save_model_configuration
from tests.integration.test_structured_llm import catalog, response
from tests.unit.test_event_enricher import _evidence_item, _seed_event


@dataclass
class AuthorizationServer:
    endpoint: str = ""
    expected_key: str = "local-good-key"
    reject: bool = False
    statuses: list[int] = field(default_factory=list)


@pytest.fixture
def authorization_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[AuthorizationServer]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    state = AuthorizationServer()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            status = 401 if state.reject or self.headers.get("Authorization") != f"Bearer {state.expected_key}" else 200
            state.statuses.append(status)
            if status == 401:
                result = {"error": {"message": "invalid test credential", "type": "authentication_error"}}
            else:
                data = json.loads(json.loads(raw)["messages"][1]["content"])
                if "candidate" in data:
                    quotes = [
                        {"evidence_id": item["evidence_id"], "quote_id": item["quotes"][0]["quote_id"]}
                        for item in data["evidence"]
                        if item["quotes"]
                    ][:16]
                    output = {
                        "judgements": [
                            {"field": name, "supported": True, "reason": "本地协议验证", "quotes": quotes}
                            for name in ("title", "summary")
                        ]
                    }
                else:
                    output = {
                        "title": "主播宣布周末挑战规则",
                        "summary": "主播宣布周末挑战规则，挑战完成后抽取奖励。",
                        "category": "announcement",
                        "entities": ["周末挑战"],
                        "semantic_confidence": 0.9,
                        "evidence_ids": ["E1"],
                    }
                result = response(json.dumps(output, ensure_ascii=False))
            encoded = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    llm.close_clients()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


def _save(server: AuthorizationServer, legacy: bool, key: str, *, name: str = "测试连接", enabled: bool = True) -> None:
    if legacy:
        llm_providers.save_providers(
            [
                llm_providers.LLMProvider(
                    id="connection",
                    name=name,
                    base_url=server.endpoint,
                    api_key=key,
                    model="model0",
                    enabled=enabled,
                )
            ]
        )
    else:
        change = catalog(revision=model_configuration_view().revision, base_url=server.endpoint)
        change.connections[0].api_key = key
        change.connections[0].name = name
        change.connections[0].enabled = enabled
        save_model_configuration(change)


def _event() -> int:
    return _seed_event(
        evidence=[
            _evidence_item(
                "evidence:asr:1",
                "asr",
                excerpt="主播宣布周末挑战规则，挑战完成后抽取奖励。",
            )
        ]
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_corrected_key_retries_unauthorized_but_preserves_success_cache(
    temp_db: None,
    authorization_server: AuthorizationServer,
    legacy: bool,
) -> None:
    server = authorization_server
    _save(server, legacy, "local-bad-key")
    event_id = _event()
    first = enrich_hotspot_event(event_id)
    assert first is not None and first.error == "unauthorized"
    before = authorization_revisions()
    _save(server, legacy, "local-bad-key", name="仅修改名称")
    save_configuration(ConfigurationChange(values={"clip_video_crf": 24}))
    assert authorization_revisions() == before
    assert enrich_hotspot_event(event_id) is None and server.statuses == [401]
    _save(server, legacy, "local-good-key")
    assert authorization_revisions() != before
    recovered = enrich_hotspot_event(event_id)
    assert recovered is not None and recovered.source == "llm"
    assert server.statuses == [401, 200, 200]
    assert recovered.bundle_fingerprint == first.bundle_fingerprint
    with get_session() as db:
        metadata = json.loads(db.get(HotspotEvent, event_id).features_json)["event_enrichment"]
        assert metadata["failure_count"] == 0 and metadata["status"] == "succeeded"
        serialized = json.dumps(metadata) + db.get(AppSetting, AUTHORIZATION_REVISIONS_KEY).value
    for key in ("local-good-key", "local-bad-key"):
        assert key not in serialized and hashlib.sha256(key.encode()).hexdigest() not in serialized
    _save(server, legacy, "local-rotated-key")
    assert enrich_hotspot_event(event_id) is None and server.statuses == [401, 200, 200]


@pytest.mark.parametrize("legacy", [False, True])
def test_old_snapshot_keeps_matching_key_revision_and_late_failure_cannot_replace_success(
    temp_db: None,
    authorization_server: AuthorizationServer,
    legacy: bool,
) -> None:
    server = authorization_server
    _save(server, legacy, "local-bad-key")
    event_id = _event()
    with settings_scope():
        provider = llm_providers.active_providers()[0]
        draft = compute_event_enrichment(event_id)
        assert draft is not None and draft.error == "unauthorized"
        _save(server, legacy, "local-good-key")
        old = llm_providers.active_providers()[0]
        assert old.api_key == provider.api_key and old.authorization_revision == provider.authorization_revision
        with get_session() as db:
            assert not commit_event_enrichment(db, draft)
    assert enrich_hotspot_event(event_id).source == "llm"
    with get_session() as db:
        assert not commit_event_enrichment(db, draft)
        assert json.loads(db.get(HotspotEvent, event_id).features_json)["event_enrichment"]["status"] == "succeeded"


def test_same_authorization_late_unauthorized_cannot_replace_verified_success(
    temp_db: None,
    authorization_server: AuthorizationServer,
) -> None:
    server = authorization_server
    _save(server, False, "local-good-key")
    event_id = _event()
    server.reject = True
    draft = compute_event_enrichment(event_id)
    assert draft is not None and draft.error == "unauthorized"
    server.reject = False
    assert enrich_hotspot_event(event_id).source == "llm"
    with get_session() as db:
        assert not commit_event_enrichment(db, draft)
        assert db.get(HotspotEvent, event_id).title == "主播宣布周末挑战规则"


@pytest.mark.parametrize("legacy", [False, True])
def test_authorization_revision_rolls_back_with_failed_configuration_commit(
    temp_db: None,
    authorization_server: AuthorizationServer,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    server = authorization_server
    _save(server, legacy, "local-bad-key")
    before = authorization_revisions()
    original = Session.commit

    def fail_commit(db: Session) -> None:
        if any(isinstance(row, AppSetting) and row.key == "configuration_revision" for row in [*db.new, *db.dirty]):
            raise OperationalError("controlled configuration write failure", {}, OSError("storage unavailable"))
        original(db)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(OperationalError):
            _save(server, legacy, "local-good-key")
    with settings_scope(refresh=False):
        assert authorization_revisions() == before
        assert llm_providers.active_providers()[0].api_key == "local-bad-key"
    assert authorization_revisions() == before
    assert llm_providers.active_providers()[0].api_key == "local-bad-key"


def test_legacy_zero_revision_and_deleted_connection_identity_are_preserved(temp_db: None) -> None:
    provider = llm_providers.LLMProvider(id="existing", name="旧配置", base_url="", api_key="old", model="model")
    # 安装前已存在的配置保持零身份，不因补充记录或显示名称触发收费恢复。
    with get_session() as db:
        db.add(AppSetting(key="llm_providers", value=json.dumps([provider.to_dict()])))
    provider.name = "仅名称改变"
    llm_providers.save_providers([provider])
    assert authorization_revisions() == {}
    llm_providers.save_providers([])
    deleted = authorization_revisions()["legacy-existing"]
    llm_providers.save_providers([provider])
    assert authorization_revisions()["legacy-existing"] > deleted
    with get_session() as db:
        assert len(db.exec(select(AppSetting).where(AppSetting.key == AUTHORIZATION_REVISIONS_KEY)).all()) == 1


def test_legacy_merge_restores_old_failure_only_after_actual_credential_change(
    temp_db: None,
    authorization_server: AuthorizationServer,
) -> None:
    server = authorization_server
    provider = llm_providers.LLMProvider(
        id="old", name="旧连接", base_url=server.endpoint, api_key="local-bad-key", model="model0"
    )
    with get_session() as db:
        db.add(AppSetting(key="llm_providers", value=json.dumps([provider.to_dict()])))
    event_id = _event()
    assert enrich_hotspot_event(event_id).error == "unauthorized"
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        features = json.loads(event.features_json)
        del features["event_enrichment"]["authorization_identity"]
        event.features_json = json.dumps(features)
    incoming = {**provider.to_dict(), "name": "更改显示名称", "api_key": "****"}
    llm_providers.merge_and_save([incoming])
    assert authorization_revisions() == {}
    assert enrich_hotspot_event(event_id) is None and server.statuses == [401]
    llm_providers.merge_and_save([{**incoming, "api_key": "local-good-key"}])
    assert enrich_hotspot_event(event_id).source == "llm"
    assert server.statuses == [401, 200, 200]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("change", ["enabled", "endpoint"])
def test_connection_authorization_changes_allow_recovery_but_equivalent_endpoint_does_not(
    temp_db: None,
    authorization_server: AuthorizationServer,
    legacy: bool,
    change: str,
) -> None:
    server = authorization_server
    _save(server, legacy, "local-good-key")
    event_id = _event()
    server.reject = True
    assert enrich_hotspot_event(event_id).error == "unauthorized"
    before = authorization_revisions()
    server.endpoint += "/"
    _save(server, legacy, "local-good-key")
    assert authorization_revisions() == before
    server.endpoint = server.endpoint.rstrip("/")
    _save(server, legacy, "local-good-key")
    assert enrich_hotspot_event(event_id) is None and server.statuses == [401]
    if change == "enabled":
        _save(server, legacy, "local-good-key", enabled=False)
        _save(server, legacy, "local-good-key", enabled=True)
    else:
        server.endpoint = server.endpoint.removesuffix("/v1") + "/v2"
        _save(server, legacy, "local-good-key")
    assert authorization_revisions() != before
    server.reject = False
    assert enrich_hotspot_event(event_id).source == "llm"
    assert server.statuses == [401, 200, 200]
