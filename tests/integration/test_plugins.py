from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _write_web_plugin(root: Path) -> None:
    directory = root / "web-demo"
    directory.mkdir(parents=True)
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": "web-demo",
                "name": "Web 示例",
                "version": "2.0.0",
                "api_version": "1",
                "entrypoint": "main.py:Plugin",
                "description": "Web API 测试插件",
                "settings_page": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (directory / "main.py").write_text(
        """from app.plugins import BasePlugin, PluginSetting

class Plugin(BasePlugin):
    settings_schema = (
        PluginSetting(key="notice", label="通知文字", default="hello", required=True),
        PluginSetting(key="active", label="活动", kind="boolean", default=True),
    )
""",
        encoding="utf-8",
    )


def test_plugin_center_api_and_settings_page(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.web.main import app

    _write_web_plugin(tmp_path)
    monkeypatch.setattr(settings, "plugin_dir", str(tmp_path))

    with TestClient(app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert 'data-tab="plugins">插件</button>' in dashboard.text
        assert 'id="tab-plugins"' in dashboard.text

        listing = client.get("/api/plugins")
        assert listing.status_code == 200
        listing_data = listing.json()
        assert listing_data["plugins"], listing_data
        plugin = listing_data["plugins"][0]
        assert plugin["id"] == "web-demo"
        assert plugin["loaded"] is False

        settings_before_enable = client.get("/api/plugins/web-demo/settings")
        assert settings_before_enable.status_code == 409

        enabled = client.patch("/api/plugins/web-demo", json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["loaded"] is True

        settings_page = client.get("/plugins/web-demo/settings")
        assert settings_page.status_code == 200
        assert "Web 示例" in settings_page.text
        assert "/static/js/plugin_settings.js" in settings_page.text

        saved = client.patch(
            "/api/plugins/web-demo/settings",
            json={"values": {"notice": "已保存", "active": False}},
        )
        assert saved.status_code == 200
        fields = {field["key"]: field["value"] for field in saved.json()["fields"]}
        assert fields == {"notice": "已保存", "active": False}

        disabled = client.patch("/api/plugins/web-demo", json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["loaded"] is False


def test_plugin_frontend_modules_are_served(temp_db: None) -> None:
    from app.web.main import app

    with TestClient(app) as client:
        for path in ("/static/js/plugins.js", "/static/js/plugin_settings.js"):
            response = client.get(path)
            assert response.status_code == 200
            assert "javascript" in response.headers["content-type"]
