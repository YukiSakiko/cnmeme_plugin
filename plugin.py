"""提供独立的内部查词工具，不发送消息或接入本地黑话库。"""

from typing import Any, Dict, Optional

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

import aiohttp
import asyncio

from .client import CnMemeAPIError, CnMemeClient
from .config import CnMemePluginConfig


class CnMemePlugin(MaiBotPlugin):
    config_model = CnMemePluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[CnMemeClient] = None

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
        )

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
        if not self.config.plugin.enabled:
            return {"success": False, "content": "梗百科插件已停用。"}
        if self._client is None:
            raise RuntimeError("梗百科插件尚未加载。")
        try:
            return await self._client.lookup(query)
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


def create_plugin() -> CnMemePlugin:
    return CnMemePlugin()
