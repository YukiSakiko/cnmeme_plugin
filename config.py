"""插件配置，不依赖主程序的黑话设置。"""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件设置"

    config_version: str = Field(default="0.1.0", description="配置版本")
    enabled: bool = Field(default=True, description="是否启用插件")


class LookupSection(PluginConfigBase):
    __ui_label__ = "查词设置"

    timeout_seconds: float = Field(default=20.0, ge=1, le=45, description="单次查词总等待时间，包含排队时间，单位为秒")
    max_results: int = Field(default=3, ge=1, le=5, description="最多提供多少个候选词条")
    max_content_length: int = Field(default=3000, ge=200, le=10000, description="每个词条正文最多保留的字数")
    cache_ttl_seconds: int = Field(default=60, ge=0, le=600, description="查询结果缓存时间，单位为秒；设为零时不缓存")


class CnMemePluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    lookup: LookupSection = Field(default_factory=LookupSection)
