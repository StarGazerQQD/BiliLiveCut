from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.configuration_policy import configuration_policy


def test_trusted_frontend_proxies_are_explicit_deployment_configuration() -> None:
    assert Settings(_env_file=None).frontend_trusted_proxy_ips == ""
    config = Settings(_env_file=None, frontend_trusted_proxy_ips="127.0.0.1, ::1, 192.0.2.0/24")
    assert config.frontend_trusted_proxy_ips == "127.0.0.1/32,::1/128,192.0.2.0/24"
    assert configuration_policy("frontend_trusted_proxy_ips").mode == "restart"


@pytest.mark.parametrize("value", ["*", "proxy.example", "127.0.0.1,", "192.0.2.0/99"])
def test_invalid_trusted_proxy_network_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="frontend_trusted_proxy_ips"):
        Settings(_env_file=None, frontend_trusted_proxy_ips=value)
