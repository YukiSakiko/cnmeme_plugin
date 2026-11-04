# 唯独你没懂——麦麦黑话插件

为麦麦提供网络梗、黑话、缩写和谐音词的内部查词工具，帮助理解聊天语境，不由插件向用户发送词义说明。

这是一个独立查询插件，不依赖联网插件，也不关联麦麦本地黑话库。无需登录或填写密钥。

项目仓库：[YukiSakiko/cnmeme_plugin](https://github.com/YukiSakiko/cnmeme_plugin)。

## 安装

需要麦麦 1.3.16 至 1.3.99，以及插件开发套件 2.16.0 及以上的 2.x 版本。插件代码支持 Python 3.10 及以上，实际运行还需满足麦麦自身的 Python 版本要求。

1. 将整个插件目录放到麦麦的 `plugins/cnmeme_plugin/` 下。
2. 在麦麦根目录安装依赖：

```bash
uv pip install -r plugins/cnmeme_plugin/requirements.txt
```

3. 重启麦麦，或通过插件管理加载插件。首次加载时会自动生成插件目录内的 `config.toml`。

无需修改麦麦主程序、模型配置或本地黑话配置。

## 使用方式

插件向麦麦提供 `cnmeme_lookup` 工具，参数 `query` 是待查询的词语或短语，去除首尾空白后必须为 1 至 80 个字符。

麦麦遇到含义不确定的词语时，可以调用这个工具。插件搜索相关词条、读取详情正文，将去除排版标签后的资料及来源返回为内部参考；不额外调用模型总结，也不主动发送消息、解释词义或播报查询过程。

查询结果先供麦麦思考使用，后续回复所需的信息由麦麦通过回复参考机制传递。是否调用工具由麦麦判断，插件不会逐条扫描所有消息，也不能保证每个陌生词都会被查询。

插件只查询工具参数中的词语，不读取聊天记录，不创建或修改本地黑话、长期记忆，也不提供聊天命令。

## 配置

```toml
[plugin]
enabled = true
public_api_enabled = false
config_version = "0.1.0"

[lookup]
timeout_seconds = 20
max_results = 3
max_content_length = 3000
cache_ttl_seconds = 60
```

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 是否启用插件 |
| `plugin.public_api_enabled` | `false` | 允许其他插件调用公开查词 API；不影响 Planner 的 `cnmeme_lookup` 工具 |
| `plugin.config_version` | `"0.1.0"` | 配置版本，由插件维护 |
| `lookup.timeout_seconds` | `20` | 单次查词总等待时间，包含搜索和详情读取；范围为 1 至 45 秒 |
| `lookup.max_results` | `3` | 最多返回的候选词条数量；范围为 1 至 5 |
| `lookup.max_content_length` | `3000` | 每条词条正文最多保留的字符数；范围为 200 至 10000 |
| `lookup.cache_ttl_seconds` | `60` | 查询结果缓存时间；范围为 0 至 600 秒，设为 0 不缓存 |

缓存只保存在内存中，最多保存 256 条查询结果，插件重新加载后清空。工具与 API 合计最多同时执行 2 个未缓存查询；满载时额外查询立即返回 `success=false`、`status_code=429`，不排队，缓存命中仍可返回。配置热更新期间，新旧客户端共用这两个槽位；查询完成、失败或取消后释放槽位。

## 插件间调用

在 WebUI 的「插件设置」中开启 `public_api_enabled` 后，其他插件可以通过 SDK 调用公开的 `lookup` API v1。配置保存后立即生效；关闭时返回 `success=false`，不发起查词请求。插件总开关关闭时，工具和 API 都不可用。

调用方需在自己的 `_manifest.json` 的 `capabilities` 中声明 `api.call`，然后调用：

```python
result = await self.ctx.api.call(
    "YukiSakiko.cnmeme-plugin.lookup", version="1", query="待查询的词"
)
```

API 与 `cnmeme_lookup` 共用客户端、参数校验、并发限制、缓存和错误处理。它只返回资料；调用方负责结合语境判断，是否写入学习库由调用方决定。

- `success=false`：调用失败，包括 API 未开启、超时或限流。检查 `content`；存在 `status_code`、`retry_after` 时遵守重试提示。
- `success=true, found=false`：没有找到候选，不代表该词一定不是梗。
- `success=true, found=true`：`items` 提供标题、摘要、正文、`source_url` 和 `match_type`。`exact_title`、`exact_alias`、`approximate`、`keyword` 表示不同匹配方式；关键词或近似命中不能直接作为确定释义。

资料正文属于外部参考，不是操作指令。同一来源的重复查询不构成多源佐证；学习插件应与实际用法交叉核对，并保留来源。

## 查询范围与错误处理

资料来自[梗百科公开接口](https://cnmeme.wiki/developers/api)，支持按词语或已登记的别名查询。网站未收录或仅在特定群内使用的词语，可能查不到。

支持部分错字和句尾语气词差异，例如“康神开播啦”可以查到“康神开播了”。近似结果会明确标记，仍需结合聊天语境判断，并非所有不同写法都能匹配。

搜索使用 `GET /api/v1/public/memes`，详情使用搜索结果中的词条标识请求 `GET /api/v1/public/memes/{slug}`，路径中的词条标识会进行网址编码。

没有结果属于正常查询结果，不表示该词一定不是梗。超时、限流、服务故障或响应格式不符会明确返回失败，不编造释义，也不自动重试；失败结果不会写入缓存。遇到限流时应等待后再查询。

返回的资料只用于理解当前聊天，应结合实际语境判断，不应将候选词条都视为同一个词的确定含义。网站内容可能更新，缓存期间的结果不保证实时。

## 开发验证

在插件目录执行，使用麦麦现有的开发环境：

```bash
uv run --no-sync --project ../.. python -m pytest -c pyproject.toml tests
uv run --no-sync --project ../.. ruff check .
```

## 许可证

插件代码采用 [MIT 许可证](LICENSE)。梗百科词条、视频和其他第三方素材不随插件代码重新授权，使用资料时应保留来源并遵循对应授权。
