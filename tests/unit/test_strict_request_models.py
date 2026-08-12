"""Web 写入请求必须拒绝未知字段。"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from pydantic import BaseModel

from app.web import routers


def test_all_router_request_models_forbid_unknown_fields() -> None:
    """任何路由请求模型都不得静默接纳旧字段或拼写错误字段。"""
    relaxed: list[str] = []
    for module_info in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"{routers.__name__}.{module_info.name}")
        for name, model in inspect.getmembers(module, inspect.isclass):
            if model.__module__ != module.__name__ or not issubclass(model, BaseModel):
                continue
            if model.model_config.get("extra") != "forbid":
                relaxed.append(f"{module.__name__}.{name}")

    assert relaxed == []
