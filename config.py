"""插件的查词与自动收录设置。"""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件设置"

    config_version: str = Field(
        default="0.1.0",
        description="配置版本",
        json_schema_extra={"label": "配置版本", "hint": "由插件维护，不需要手动修改。"},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件", "hint": "关闭后，麦麦和其他插件都不能用梗百科查词。"},
    )
    public_api_enabled: bool = Field(
        default=False,
        description="允许其他插件使用梗百科查词，不影响麦麦自己查词",
        json_schema_extra={
            "label": "允许其他插件查词",
            "hint": "开启后，其他插件也能使用梗百科查词。关闭不影响麦麦自己查词。",
        },
    )


class LookupSection(PluginConfigBase):
    __ui_label__ = "查词设置"

    timeout_seconds: float = Field(
        default=20.0,
        ge=1,
        le=45,
        description="一次查词最多等待的时间，单位为秒",
        json_schema_extra={"label": "查词等待上限", "hint": "单位为秒。超过这个时间还没查完，就结束本次查询。"},
    )
    max_results: int = Field(
        default=3,
        ge=1,
        le=5,
        description="一次查词最多返回的词条数",
        json_schema_extra={"label": "最多返回词条数", "hint": "查到多个相关词条时，最多给麦麦参考几条。"},
    )
    max_content_length: int = Field(
        default=3000,
        ge=200,
        le=10000,
        description="每个词条正文最多保留的字符数",
        json_schema_extra={"label": "词条正文长度上限", "hint": "按字符数计算。正文超过上限时，只保留前面的内容。"},
    )
    cache_ttl_seconds: int = Field(
        default=60,
        ge=0,
        le=600,
        description="查词结果保留时间，单位为秒；设为 0 时每次重新查",
        json_schema_extra={
            "label": "查词结果保留时间",
            "hint": "单位为秒。这段时间内重复查同一个词，会沿用上次结果。设为 0 时每次重新查。",
        },
    )
    save_global_jargon: bool = Field(
        default=False,
        description="将麦麦查到的明确释义自动收录为全局黑话",
        json_schema_extra={
            "label": "自动收录全局黑话",
            "hint": "开启后，明确的释义会存入黑话库，供启用了黑话功能的会话使用，不覆盖已有释义。",
        },
    )


class CnMemePluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    lookup: LookupSection = Field(default_factory=LookupSection)
