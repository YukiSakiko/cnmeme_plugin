"""使用真实 SDK 的配置与组件收集接口验证插件边界。"""

from unittest.mock import AsyncMock
from types import SimpleNamespace

import asyncio

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF

import aiohttp
import pytest

from cnmeme_plugin import plugin as plugin_module
from cnmeme_plugin.client import CnMemeAPIError, CnMemeClient, ConfirmedDefinition, UsageExample
from cnmeme_plugin.plugin import CnMemePlugin, create_plugin


@pytest.fixture
def plugin() -> CnMemePlugin:
    instance = create_plugin()
    instance.set_plugin_config(instance.build_default_config())
    return instance


def test_settings_schema_exposes_chinese_labels_and_hints(plugin: CnMemePlugin) -> None:
    schema = plugin.get_webui_config_schema()
    fields = {name: field for section in schema["sections"].values() for name, field in section["fields"].items()}
    labels = {
        "config_version": "配置版本",
        "enabled": "启用插件",
        "public_api_enabled": "允许其他插件查词",
        "timeout_seconds": "查词等待上限",
        "max_results": "最多返回词条数",
        "max_content_length": "词条正文长度上限",
        "cache_ttl_seconds": "查词结果保留时间",
        "save_global_jargon": "自动收录全局黑话",
        "save_global_expression": "自动收录全局表达",
    }

    assert {name: fields[name]["label"] for name in labels} == labels
    assert all(fields[name]["hint"] for name in labels)
    assert plugin.config.lookup.save_global_expression is False


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
    assert "如果不弄懂它就难以理解当前消息，优先查词" in metadata["description"]
    assert "不影响理解的陌生词不用查" in metadata["description"]
    assert "知道常见写法时用常见写法，不确定就用原词" in metadata["parameters"][0]["description"]


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


@pytest.fixture
def global_lookup(plugin, monkeypatch):
    async def setup(*definitions, save_jargon=True, save_expression=False):
        settings = plugin.get_plugin_config_data()
        settings["plugin"]["public_api_enabled"] = True
        settings["lookup"]["save_global_jargon"] = save_jargon
        settings["lookup"]["save_global_expression"] = save_expression
        plugin.set_plugin_config(settings)
        await plugin.on_load()
        result = {"success": True, "found": True, "items": [{"title": "测试梗", "content": "外部正文"}]}
        monkeypatch.setattr(plugin._client, "lookup", AsyncMock(return_value=result))
        monkeypatch.setattr(plugin._client, "lookup_with_definitions", AsyncMock(return_value=(result, definitions)))
        db = SimpleNamespace(get=AsyncMock(return_value=[]), save=AsyncMock(return_value={"id": 1}))
        plugin._set_context(SimpleNamespace(db=db))
        return result, db

    return setup


@pytest.fixture
def definition() -> ConfirmedDefinition:
    return ConfirmedDefinition("登记别名", "明确的释义。")


@pytest.fixture
def usage_definition() -> ConfirmedDefinition:
    return ConfirmedDefinition("登记别名", "明确的释义。", UsageExample("和朋友开玩笑时", "登记别名"))


async def test_tool_saves_only_confirmed_definition_as_global(plugin, global_lookup, definition):
    result, db = await global_lookup(definition)

    assert await plugin.lookup("登记别名", stream_id="a-group") == result
    db.get.assert_awaited_once_with("Jargon", filters={"content": "登记别名", "is_global": True}, limit=1)
    db.save.assert_awaited_once_with(
        "Jargon",
        data={
            "content": "登记别名",
            "meaning": "明确的释义。",
            "session_id_dict": "{}",
            "is_jargon": True,
            "is_complete": True,
            "is_global": True,
            "created_by": "MANUAL",
        },
    )


async def test_public_api_and_unconfirmed_tool_result_do_not_access_db(plugin, global_lookup, usage_definition):
    result, db = await global_lookup(usage_definition, save_expression=True)

    assert await plugin.lookup_api("登记别名") == result
    plugin._client.lookup_with_definitions.assert_not_awaited()
    plugin._client.lookup_with_definitions.return_value = (result, ())
    assert await plugin.lookup("登记别名") == result
    db.get.assert_not_awaited()
    db.save.assert_not_awaited()


async def test_tool_saves_global_expression_independently_as_unreviewed(plugin, global_lookup, usage_definition):
    result, db = await global_lookup(usage_definition, save_jargon=False, save_expression=True)
    fields = {"situation": "和朋友开玩笑时", "style": "登记别名", "session_id": None}

    assert await plugin.lookup("登记别名", stream_id="a-group") == result

    db.get.assert_awaited_once_with("Expression", filters=fields, limit=1)
    db.save.assert_awaited_once_with("Expression", data={**fields, "content_list": "[]", "checked": False})


async def test_expression_opt_in_requires_a_usage_example(plugin, global_lookup, definition):
    result, db = await global_lookup(definition, save_jargon=False, save_expression=True)

    assert await plugin.lookup("登记别名") == result

    db.get.assert_not_awaited()
    db.save.assert_not_awaited()


async def test_jargon_opt_in_does_not_save_expression(plugin, global_lookup, usage_definition):
    result, db = await global_lookup(usage_definition)

    assert await plugin.lookup("登记别名") == result

    assert [call.args[0] for call in db.save.await_args_list] == ["Jargon"]


async def test_concurrent_lookups_do_not_insert_same_global_expression_twice(plugin, global_lookup, usage_definition):
    result, db = await global_lookup(usage_definition, save_jargon=False, save_expression=True)
    saved = []

    async def get(model, *, filters, limit):
        assert model == "Expression" and limit == 1
        return [row for row in saved if all(row[key] == value for key, value in filters.items())]

    async def save(model, *, data):
        assert model == "Expression"
        await asyncio.sleep(0)
        saved.append({"id": 1, **data})
        return saved[-1]

    db.get.side_effect = get
    db.save.side_effect = save
    assert await asyncio.gather(plugin.lookup("登记别名"), plugin.lookup("登记别名")) == [result, result]
    assert db.save.await_count == 1


@pytest.mark.parametrize("failed_model", ["Jargon", "Expression"])
async def test_jargon_and_expression_failures_are_independent(plugin, global_lookup, usage_definition, failed_model):
    result, db = await global_lookup(usage_definition, save_expression=True)

    async def save(model, *, data):
        if model == failed_model:
            raise RuntimeError("数据库不可用")
        return {"id": 1}

    db.save.side_effect = save
    assert await plugin.lookup("登记别名") == result
    assert [call.args[0] for call in db.save.await_args_list] == ["Jargon", "Expression"]


async def test_global_definition_is_not_overwritten(plugin, global_lookup, definition):
    result, db = await global_lookup(definition)
    db.get.return_value = [{"id": 1, "meaning": "原有释义", "is_global": True}]

    assert await plugin.lookup("登记别名") == result
    db.save.assert_not_awaited()


async def test_lookup_saves_other_definitions_if_one_fails(plugin, global_lookup, definition):
    other = ConfirmedDefinition("奶蛙之母", "另一条明确的释义。")
    result, db = await global_lookup(definition, other)
    db.save.side_effect = [RuntimeError("首条写入失败"), {"id": 2}]

    assert await plugin.lookup("奶蛙") == result
    assert [call.kwargs["data"]["content"] for call in db.save.await_args_list] == [definition.term, other.term]


async def test_concurrent_lookups_do_not_insert_same_global_definition_twice(plugin, global_lookup, definition):
    result, db = await global_lookup(definition)
    saved = []

    async def get(*args, **kwargs):
        return list(saved)

    async def save(model, *, data):
        await asyncio.sleep(0)
        saved.append(data)
        return data

    db.get.side_effect = get
    db.save.side_effect = save
    assert await asyncio.gather(plugin.lookup("登记别名"), plugin.lookup("登记别名")) == [result, result]
    assert db.save.await_count == 1


@pytest.mark.parametrize("failed_operation", ["get", "save"])
async def test_database_failure_does_not_change_lookup_response(
    plugin, global_lookup, definition, failed_operation, caplog
):
    result, db = await global_lookup(definition)
    getattr(db, failed_operation).side_effect = RuntimeError("数据库不可用")

    assert await plugin.lookup("登记别名") == result
    assert "未能自动收录" in caplog.text
    if failed_operation == "get":
        db.save.assert_not_awaited()


async def test_database_error_payload_is_not_treated_as_saved(plugin, global_lookup, definition, caplog):
    result, db = await global_lookup(definition)
    db.save.return_value = {"success": False, "error": "数据库不可用"}

    assert await plugin.lookup("登记别名") == result
    assert "黑话库未确认写入" in caplog.text


async def test_database_timeout_returns_reference_and_releases_lock(
    plugin, global_lookup, definition, monkeypatch, caplog
):
    result, db = await global_lookup(definition)
    blocked = asyncio.Event()

    async def blocked_get(*args, **kwargs):
        await blocked.wait()

    db.get.side_effect = blocked_get
    monkeypatch.setattr(plugin_module, "GLOBAL_SAVE_TIMEOUT_SECONDS", 0.05)

    assert await asyncio.wait_for(plugin.lookup("登记别名"), timeout=1) == result
    db.save.assert_not_awaited()
    assert "自动收录超时" in caplog.text
    assert not plugin._global_write_lock.locked()


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


async def test_config_reload_shares_capacity_with_inflight_tool_and_api_calls(plugin, monkeypatch):
    await plugin.on_load()
    original_client = plugin._client
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def search(self, session, query):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            started.set()
        try:
            await release.wait()
            return SimpleNamespace(items=[], has_more=False)
        finally:
            active -= 1

    monkeypatch.setattr(CnMemeClient, "_search", search)
    tasks = [asyncio.create_task(plugin.lookup(query)) for query in ("甲", "乙")]
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        settings = plugin.get_plugin_config_data()
        settings["plugin"]["public_api_enabled"] = True
        settings["lookup"]["max_results"] = 1
        plugin.set_plugin_config(settings)
        await plugin.on_config_update(CONFIG_RELOAD_SCOPE_SELF, settings, "0.1.0")
        assert plugin._client is not original_client
        result = await asyncio.wait_for(plugin.lookup_api("丙"), timeout=1)
        assert result["success"] is False and result["status_code"] == 429
        assert peak == 2
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        assert (await plugin.lookup_api("丁"))["success"] is True
        assert (await tasks[1])["success"] is True
        assert peak == 2 and active == 0
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await plugin.on_unload()
