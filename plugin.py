"""提供内部查词工具，可选收录明确的全局黑话。"""

import logging
from typing import Any, Dict, Optional

from maibot_sdk import API, CONFIG_RELOAD_SCOPE_SELF, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

import aiohttp
import asyncio

from .client import CnMemeAPIError, CnMemeClient, ConfirmedDefinition
from .config import CnMemePluginConfig

logger = logging.getLogger(__name__)
JARGON_SAVE_TIMEOUT_SECONDS = 3.0


class CnMemePlugin(MaiBotPlugin):
    config_model = CnMemePluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[CnMemeClient] = None
        self._lookup_slots = asyncio.Semaphore(2)
        self._jargon_write_lock = asyncio.Lock()

    async def on_load(self) -> None:
        self._build_client()

    async def on_unload(self) -> None:
        self._client = None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._build_client()

    def _build_client(self) -> None:
        settings = self.config.lookup
        self._client = CnMemeClient(
            timeout_seconds=settings.timeout_seconds,
            max_results=settings.max_results,
            max_content_length=settings.max_content_length,
            cache_ttl_seconds=settings.cache_ttl_seconds,
            lookup_slots=self._lookup_slots,
        )

    @API("lookup", description="查询网络梗候选资料，需结合语境判断相关性。", version="1", public=True)
    async def lookup_api(self, query: str) -> Dict[str, Any]:
        if not self.config.plugin.public_api_enabled:
            return {"success": False, "content": "梗百科公开查询 API 未启用。"}
        return await self._lookup(query)

    @Tool(
        "cnmeme_lookup",
        description="查询网络梗、黑话、缩写和谐音词的含义，用于理解聊天中不熟悉的词语。",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="需要理解的单个词语或短语，一至八十个字符",
                required=True,
            )
        ],
        visibility="visible",
    )
    async def lookup(self, query: str, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return await self._lookup(query, save_global=self.config.lookup.save_global_jargon)

    async def _lookup(self, query: str, *, save_global: bool = False) -> Dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "梗百科插件已停用。"}
        if self._client is None:
            raise RuntimeError("梗百科插件尚未加载。")
        definition = None
        try:
            if save_global:
                result, definition = await self._client.lookup_with_definition(query)
            else:
                result = await self._client.lookup(query)
        except ValueError as exc:
            return {"success": False, "content": str(exc)}
        except asyncio.TimeoutError:
            return {"success": False, "content": "查询梗百科超时，本次未取得词义。"}
        except CnMemeAPIError as exc:
            result: Dict[str, Any] = {"success": False, "content": str(exc)}
            if exc.status is not None:
                result["status_code"] = exc.status
            if exc.status == 429 or (exc.status is not None and exc.status >= 500):
                result["content"] += "服务暂时不可用或限制了查询频率，请稍后再查，不要立即重复请求。"
            if exc.retry_after is not None:
                result["retry_after"] = exc.retry_after
                result["content"] += f"接口要求的等待时间为 {exc.retry_after}（秒数或日期）。"
            return result
        except aiohttp.ClientError:
            return {"success": False, "content": "无法连接梗百科，本次未取得词义。"}

        if definition is not None:
            try:
                await asyncio.wait_for(self._save_global_jargon(definition), timeout=JARGON_SAVE_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("未能自动收录梗百科释义，本次查词结果仍可使用。", exc_info=True)
        return result

    async def _save_global_jargon(self, definition: ConfirmedDefinition) -> None:
        # 宿主没有按词语与范围一起去重的写入接口；同一插件内先查再插，避免并发重复收录。
        async with self._jargon_write_lock:
            if self._client is None or not self.config.plugin.enabled or not self.config.lookup.save_global_jargon:
                return
            db = self.ctx.db
            existing = await db.get("Jargon", filters={"content": definition.term, "is_global": True}, limit=1)
            if not isinstance(existing, list):
                raise RuntimeError("黑话库查询返回了未知结果。")
            if existing:
                return
            saved = await db.save(
                "Jargon",
                data={
                    "content": definition.term,
                    "meaning": f"{definition.meaning}（来源：梗百科，{definition.source_url}）",
                    "session_id_dict": "{}",
                    "is_jargon": True,
                    "is_complete": True,
                    "is_global": True,
                    # 固定外部释义，不让聊天学习流程重新推断或覆盖。
                    "created_by": "MANUAL",
                },
            )
            if not isinstance(saved, dict) or not saved.get("id"):
                raise RuntimeError("黑话库未确认写入。")


def create_plugin() -> CnMemePlugin:
    return CnMemePlugin()
