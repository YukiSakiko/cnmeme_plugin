"""使用真实 SDK 的配置与组件收集接口验证插件边界。"""

from unittest.mock import AsyncMock

import asyncio

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF

import aiohttp
import pytest

from cnmeme_plugin.client import CnMemeAPIError
from cnmeme_plugin.plugin import CnMemePlugin, create_plugin


@pytest.fixture
def plugin() -> CnMemePlugin:
    instance = create_plugin()
    instance.set_plugin_config(instance.build_default_config())
    return instance


def test_visible_tool_and_public_lookup_api_are_registered(plugin: CnMemePlugin) -> None:
    components = plugin.get_components()

    assert len(components) == 2
    tool = next(component for component in components if component["type"] == "TOOL")
    api = next(component for component in components if component["type"] == "API")
    assert api["name"] == "lookup"
    assert api["metadata"]["public"] is True
    assert api["metadata"]["version"] == "1"
    assert tool["name"] == "cnmeme_lookup"
    metadata = tool["metadata"]
    assert metadata["visibility"] == "visible"
    assert [parameter["name"] for parameter in metadata["parameters"]] == ["query"]
    assert metadata["parameters"][0]["required"] is True
    assert metadata["description"] == "查询网络梗、黑话、缩写和谐音词的含义，用于理解聊天中不熟悉的词语。"


async def test_success_returns_internal_reference_without_host_context(
    plugin: CnMemePlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await plugin.on_load()
    result = {"success": True, "found": True, "items": [{"title": "测试梗", "content": "释义"}]}
    lookup = AsyncMock(return_value=result)
    monkeypatch.setattr(plugin._client, "lookup", lookup)

    assert await plugin.lookup("测试梗", stream_id="not-used") == result
    lookup.assert_awaited_once_with("测试梗")
    assert (await plugin.lookup_api("测试梗"))["success"] is False
    lookup.assert_awaited_once_with("测试梗")
    settings = plugin.get_plugin_config_data()
    settings["plugin"]["public_api_enabled"] = True
    plugin.set_plugin_config(settings)
    await plugin.on_config_update(CONFIG_RELOAD_SCOPE_SELF, settings, "0.1.0")
    monkeypatch.setattr(plugin._client, "lookup", lookup)
    assert await plugin.lookup_api("测试梗") == result
    assert lookup.await_count == 2
    settings["plugin"]["public_api_enabled"] = False
    plugin.set_plugin_config(settings)
    assert (await plugin.lookup_api("测试梗"))["success"] is False
    assert lookup.await_count == 2
    assert "stop_after_execution" not in result


async def test_disabled_plugin_never_performs_lookup(plugin: CnMemePlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    await plugin.on_load()
    lookup = AsyncMock()
    monkeypatch.setattr(plugin._client, "lookup", lookup)
    settings = plugin.get_plugin_config_data()
    settings["plugin"]["enabled"] = False
    settings["plugin"]["public_api_enabled"] = True
    plugin.set_plugin_config(settings)

    assert (await plugin.lookup("测试梗"))["success"] is False
    assert (await plugin.lookup_api("测试梗"))["success"] is False
    lookup.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected_text"),
    [
        (ValueError("查询参数无效"), "查询参数无效"),
        (asyncio.TimeoutError(), "超时"),
        (aiohttp.ClientConnectionError("sensitive endpoint"), "无法连接"),
        (CnMemeAPIError("接口暂不可用", status=503), "接口暂不可用"),
    ],
)
async def test_known_errors_are_explicit_failures(
    plugin: CnMemePlugin, monkeypatch: pytest.MonkeyPatch, error: Exception, expected_text: str
) -> None:
    await plugin.on_load()
    monkeypatch.setattr(plugin._client, "lookup", AsyncMock(side_effect=error))

    result = await plugin.lookup("测试梗")

    assert result["success"] is False
    assert expected_text in result["content"]
    assert "sensitive endpoint" not in result["content"]
    assert "found" not in result


async def test_rate_limit_preserves_wait_instruction(plugin: CnMemePlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    await plugin.on_load()
    monkeypatch.setattr(
        plugin._client, "lookup", AsyncMock(side_effect=CnMemeAPIError("请求过于频繁", status=429, retry_after="30"))
    )

    result = await plugin.lookup("测试梗")

    assert result["success"] is False
    assert result["status_code"] == 429
    assert result["retry_after"] == "30"
    assert "不要立即重复" in result["content"]
    assert "30" in result["content"]


async def test_unexpected_programming_error_is_not_hidden(
    plugin: CnMemePlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await plugin.on_load()
    monkeypatch.setattr(plugin._client, "lookup", AsyncMock(side_effect=RuntimeError("unexpected bug")))
    with pytest.raises(RuntimeError, match="unexpected bug"):
        await plugin.lookup("测试梗")


async def test_lifecycle_and_self_config_reload_rebuild_client(plugin: CnMemePlugin) -> None:
    await plugin.on_load()
    original_client = plugin._client
    assert original_client is not None
    await plugin.on_config_update("model", {}, "0.1.0")
    assert plugin._client is original_client

    settings = plugin.get_plugin_config_data()
    settings["lookup"]["max_results"] = 1
    settings["lookup"]["timeout_seconds"] = 5
    plugin.set_plugin_config(settings)
    await plugin.on_config_update(CONFIG_RELOAD_SCOPE_SELF, settings, "0.1.0")
    assert plugin._client is not original_client
    assert plugin._client.max_results == 1
    assert plugin._client.timeout_seconds == 5

    await plugin.on_unload()
    assert plugin._client is None
    with pytest.raises(RuntimeError, match="尚未加载"):
        await plugin.lookup("测试梗")
