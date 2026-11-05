"""使用模拟传输验证公开接口契约，不访问公网。"""

from collections import deque
from copy import deepcopy
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import asyncio

import pytest

from cnmeme_plugin import client as client_module
from cnmeme_plugin.client import CnMemeAPIError, CnMemeClient, SEARCH_URL, html_to_text


def meme(slug: str = "测试梗", **fields: Any) -> Dict[str, Any]:
    return {"slug": slug, "title": slug, "summary": "简短摘要", "tags": ["测试"], **fields}


def page(items: List[Dict[str, Any]], *, has_more: bool = False) -> Dict[str, Any]:
    return {"schema_version": 1, "items": items, "has_more": has_more}


def detail(slug: str = "测试梗", **fields: Any) -> Dict[str, Any]:
    return {"schema_version": 1, "item": meme(slug, **{"content_html": "<p>完整释义</p>", **fields})}


class FakeResponse:
    def __init__(
        self,
        body: Any = None,
        *,
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
        error: Optional[Exception] = None,
        gate: Optional[asyncio.Event] = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.error = error
        self.gate = gate

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def json(self) -> Any:
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return deepcopy(self.body)


class FakeSession:
    def __init__(self, transport: "FakeHTTP") -> None:
        self.transport = transport

    async def __aenter__(self) -> "FakeSession":
        self.transport.active_sessions += 1
        self.transport.max_active_sessions = max(self.transport.max_active_sessions, self.transport.active_sessions)
        if self.transport.active_sessions == 2:
            self.transport.two_started.set()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.transport.active_sessions -= 1
        self.transport.closed_sessions += 1

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.transport.calls.append((url, kwargs))
        assert self.transport.responses, f"未安排的请求：{url}"
        return self.transport.responses.popleft()


class FakeHTTP:
    def __init__(self) -> None:
        self.responses: deque[FakeResponse] = deque()
        self.calls: List[Any] = []
        self.session_settings: List[Dict[str, Any]] = []
        self.active_sessions = 0
        self.max_active_sessions = 0
        self.closed_sessions = 0
        self.two_started = asyncio.Event()

    def session(self, **kwargs: Any) -> FakeSession:
        self.session_settings.append(kwargs)
        return FakeSession(self)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHTTP:
    transport = FakeHTTP()
    monkeypatch.setattr(client_module.aiohttp, "ClientSession", transport.session)
    return transport


def test_html_to_text_preserves_words_and_paragraphs() -> None:
    assert html_to_text(
        "<h2>含义</h2><p>一个<strong>网络</strong>梗 &amp; 用法。</p><ul><li>第一项<br/>换行</li></ul>"
    ) == ("含义\n一个网络梗 & 用法。\n第一项\n换行")


async def test_exact_title_ignores_body_only_hits_and_quotes_slug(http: FakeHTTP) -> None:
    slug = "测试/梗 #?"
    http.responses.extend(
        [
            FakeResponse(page([meme("仅正文提及"), meme("别名词条", aliases=["目标词"]), meme(slug, title="目标词")])),
            FakeResponse(detail(slug, title="目标词")),
        ]
    )

    result = await CnMemeClient().lookup(" 目标词 ")

    assert result["success"] is True and result["found"] is True
    assert result["query"] == "目标词"
    assert result["searched_queries"] == ["目标词"]
    assert result["matched_query"] == "目标词"
    assert len(result["items"]) == 1
    assert result["items"][0]["match_type"] == "exact_title"
    assert result["items"][0]["source_url"] == f"{SEARCH_URL}/{quote(slug, safe='')}"
    assert "目标词" in result["content"]
    assert "完整释义" in result["content"]
    assert result["items"][0]["source_url"] in result["content"]
    assert http.calls[0] == (SEARCH_URL, {"params": {"q": "目标词", "limit": 10}, "allow_redirects": False})
    assert len(http.calls) == 2
    assert http.active_sessions == 0 and http.closed_sessions == 1


async def test_keyword_candidates_are_limited_and_body_is_truncated(http: FakeHTTP) -> None:
    http.responses.extend(
        [
            FakeResponse(page([meme("候选一"), meme("候选二")], has_more=True)),
            FakeResponse(detail("候选一", content_html="<p>一二三四五六</p>")),
        ]
    )

    result = await CnMemeClient(max_results=1, max_content_length=4).lookup("相关词")

    assert result["has_more"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["content"] == "一二三四"
    assert result["items"][0]["truncated"] is True
    assert result["items"][0]["match_type"] == "keyword"


async def test_empty_results_are_successful_and_cached(http: FakeHTTP) -> None:
    http.responses.extend(FakeResponse(page([])) for _ in range(3))
    client = CnMemeClient()

    result = await client.lookup("未收录词")
    cached = await client.lookup("未收录词")

    assert result == cached
    assert result["success"] is True and result["found"] is False
    assert result["items"] == []
    assert result["searched_queries"] == ["未收录词", "未收", "录词"]
    assert result["matched_query"] is None
    assert len(http.calls) == 3


async def test_sentence_particle_variant_finds_approximate_title_and_stops(http: FakeHTTP) -> None:
    query = "康神开播啦"
    title = "康神开播了"
    http.responses.extend([FakeResponse(page([])), FakeResponse(page([meme(title)])), FakeResponse(detail(title))])

    result = await CnMemeClient().lookup(query)

    assert result["query"] == query
    assert result["searched_queries"] == [query, "康神开播"]
    assert result["matched_query"] == "康神开播"
    assert result["items"][0]["match_type"] == "approximate"
    assert result["items"][0]["title"] == title
    assert "近似" in result["content"]
    assert "康神开播" in result["content"]
    assert title in result["content"] and "完整释义" in result["content"]
    assert result["items"][0]["source_url"] in result["content"]
    assert len(http.calls) == 3


@pytest.mark.parametrize("query", ["鸡拟太美", "坤你太美"])
async def test_typo_in_middle_or_first_character_uses_other_half(http: FakeHTTP, query: str) -> None:
    http.responses.extend(
        [
            FakeResponse(page([])),
            FakeResponse(page([])),
            FakeResponse(page([meme("鸡你太美")])),
            FakeResponse(detail("鸡你太美")),
        ]
    )

    result = await CnMemeClient().lookup(query)

    assert result["query"] == query
    assert result["searched_queries"] == [query, query[:2], "太美"]
    assert result["matched_query"] == "太美"
    assert result["items"][0]["match_type"] == "approximate"
    assert [call[1]["params"]["q"] for call in http.calls if call[0] == SEARCH_URL] == result["searched_queries"]


async def test_exact_alias_is_selected_without_expanding_original_search(http: FakeHTTP) -> None:
    http.responses.extend(
        [
            FakeResponse(page([meme("正文提及"), meme("正式标题", aliases=["登记别名"])])),
            FakeResponse(detail("正式标题", aliases=["登记别名"])),
        ]
    )

    result = await CnMemeClient(max_results=1).lookup("登记别名")

    assert result["searched_queries"] == ["登记别名"]
    assert result["matched_query"] == "登记别名"
    assert result["items"][0]["title"] == "正式标题"
    assert result["items"][0]["match_type"] == "exact_alias"
    assert http.calls[1][0] == f"{SEARCH_URL}/{quote('正式标题', safe='')}"
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    ("title", "aliases", "query", "expected_term"),
    [
        ("Meme Name", [], "  meme name  ", "Meme Name"),
        ("正式标题", ["无关别名", "Meme Alias"], "meme alias", "Meme Alias"),
    ],
)
async def test_exact_definition_uses_fresh_summary_and_matched_spelling(
    http: FakeHTTP, title: str, aliases: List[str], query: str, expected_term: str
) -> None:
    item = meme("entry", title=title, aliases=aliases)
    http.responses.extend(
        [
            FakeResponse(page([item, item])),
            FakeResponse(detail("entry", title=title, aliases=aliases, summary="  指分享好消息。   也可用于自嘲。  ")),
        ]
    )
    client = CnMemeClient()

    lookup = await client.lookup(query)
    result, definition = await client.lookup_with_definition(query)

    assert result == lookup
    assert definition is not None
    assert definition.term == expected_term
    assert definition.meaning == "指分享好消息。 也可用于自嘲。"
    assert definition.source_url == f"{SEARCH_URL}/entry"
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    ("other_items", "has_more"),
    [
        ([meme("同名条目", title="目标词")], False),
        ([meme("另一个词", aliases=["目标词"])], False),
        ([], True),
    ],
)
async def test_exact_definition_requires_an_unambiguous_complete_search_page(
    http: FakeHTTP, other_items: List[Dict[str, Any]], has_more: bool
) -> None:
    http.responses.extend(
        [
            FakeResponse(page([meme("目标词"), *other_items], has_more=has_more)),
            FakeResponse(detail("目标词")),
        ]
    )

    result, definition = await CnMemeClient(max_results=1).lookup_with_definition("目标词")

    assert result["found"] is True and len(result["items"]) == 1
    assert definition is None


@pytest.mark.parametrize(
    ("search_item", "detail_response", "found"),
    [
        (meme("原词条", title="目标词"), detail("原词条", title="已改名"), True),
        (meme("原词条", aliases=["目标词"]), detail("原词条"), True),
        (meme("原词条", title="目标词"), detail("其他词条", title="目标词"), True),
        (meme("原词条", title="目标词"), None, False),
    ],
)
async def test_exact_definition_rechecks_the_entry_in_fresh_detail(
    http: FakeHTTP, search_item: Dict[str, Any], detail_response: Optional[Dict[str, Any]], found: bool
) -> None:
    http.responses.extend(
        [
            FakeResponse(page([search_item])),
            FakeResponse(detail_response) if detail_response is not None else FakeResponse(status=404),
        ]
    )

    result, definition = await CnMemeClient().lookup_with_definition("目标词")

    assert result["found"] is found
    assert definition is None


@pytest.mark.parametrize("summary", ["   ", "测试梗", "<b>释义</b>", "含有\x00控制字符", "字" * 501])
async def test_exact_definition_skips_unsuitable_summaries(http: FakeHTTP, summary: str) -> None:
    http.responses.extend([FakeResponse(page([meme()])), FakeResponse(detail(summary=summary))])

    result, definition = await CnMemeClient().lookup_with_definition("测试梗")

    assert result["found"] is True
    assert result["items"][0]["content"] == "完整释义"
    assert definition is None


@pytest.mark.parametrize(
    ("query", "item", "is_approximate", "match_type"),
    [
        ("梗", meme("梗"), False, "exact_title"),
        ("测试梗", meme("正文提及", summary="测试梗"), False, "keyword"),
        ("鸡拟太美", meme("鸡你太美"), True, "approximate"),
    ],
)
async def test_exact_definition_excludes_single_characters_and_nonexact_matches(
    http: FakeHTTP, query: str, item: Dict[str, Any], is_approximate: bool, match_type: str
) -> None:
    if is_approximate:
        http.responses.append(FakeResponse(page([])))
    http.responses.extend([FakeResponse(page([item])), FakeResponse(detail(item["slug"]))])

    result, definition = await CnMemeClient().lookup_with_definition(query)

    assert result["items"][0]["match_type"] == match_type
    assert definition is None


@pytest.mark.parametrize(
    "nearby",
    [meme("鸡你太美"), meme("正式词条", aliases=["鸡你太美"])],
)
async def test_original_results_rank_near_names_before_body_hits_without_expanding(
    http: FakeHTTP, nearby: Dict[str, Any]
) -> None:
    body_hit = meme("仅正文提及", summary="鸡你太美")
    http.responses.extend(
        [
            FakeResponse(page([body_hit, nearby])),
            FakeResponse(detail(nearby["slug"])),
            FakeResponse(detail(body_hit["slug"])),
        ]
    )

    result = await CnMemeClient().lookup("鸡拟太美")

    assert result["searched_queries"] == ["鸡拟太美"]
    assert result["matched_query"] == "鸡拟太美"
    assert [item["slug"] for item in result["items"]] == [nearby["slug"], body_hit["slug"]]
    assert all(item["match_type"] == "keyword" for item in result["items"])
    assert [call[0] for call in http.calls[1:]] == [
        f"{SEARCH_URL}/{quote(nearby['slug'], safe='')}",
        f"{SEARCH_URL}/{quote(body_hit['slug'], safe='')}",
    ]


async def test_short_query_does_not_expand(http: FakeHTTP) -> None:
    http.responses.append(FakeResponse(page([])))

    result = await CnMemeClient().lookup("原神")

    assert result["found"] is False
    assert result["searched_queries"] == ["原神"]
    assert result["matched_query"] is None
    assert len(http.calls) == 1


async def test_supplement_rejects_two_edits_and_body_or_tag_similarity(http: FakeHTTP) -> None:
    unrelated = meme("鸡你好美", summary="鸡你太美", tags=["鸡你太美"])
    http.responses.extend([FakeResponse(page([])), FakeResponse(page([unrelated])), FakeResponse(page([]))])

    result = await CnMemeClient().lookup("鸡拟太美")

    assert result["success"] is True and result["found"] is False
    assert result["items"] == []
    assert result["searched_queries"] == ["鸡拟太美", "鸡拟", "太美"]
    assert result["matched_query"] is None
    assert all(call[0] == SEARCH_URL for call in http.calls)


async def test_registered_alias_can_qualify_approximate_supplement(http: FakeHTTP) -> None:
    alias_item = meme("篮球问候语", aliases=["鸡你太美"])
    http.responses.extend(
        [
            FakeResponse(page([])),
            FakeResponse(page([alias_item])),
            FakeResponse(detail("篮球问候语", aliases=["鸡你太美"])),
        ]
    )

    result = await CnMemeClient().lookup("鸡拟太美")

    assert result["searched_queries"] == ["鸡拟太美", "鸡拟"]
    assert result["matched_query"] == "鸡拟"
    assert result["items"][0]["title"] == "篮球问候语"
    assert result["items"][0]["match_type"] == "approximate"
    assert len(http.calls) == 3


@pytest.mark.parametrize("title", ["鸡拟美", "鸡拟很太美"])
async def test_approximate_supplement_allows_one_character_deletion_or_insertion(http: FakeHTTP, title: str) -> None:
    http.responses.extend([FakeResponse(page([])), FakeResponse(page([meme(title)])), FakeResponse(detail(title))])

    result = await CnMemeClient().lookup("鸡拟太美")

    assert result["found"] is True
    assert result["searched_queries"] == ["鸡拟太美", "鸡拟"]
    assert result["items"][0]["title"] == title
    assert result["items"][0]["match_type"] == "approximate"


async def test_empty_long_query_uses_at_most_three_distinct_supplements(http: FakeHTTP) -> None:
    http.responses.extend(FakeResponse(page([])) for _ in range(4))

    result = await CnMemeClient().lookup("康神今日开播啦")

    assert result["found"] is False
    assert result["searched_queries"] == ["康神今日开播啦", "康神今日开播", "康神今", "日开播啦"]
    assert result["matched_query"] is None
    assert len(http.calls) == 4


async def test_duplicate_half_queries_are_only_searched_once(http: FakeHTTP) -> None:
    http.responses.extend(FakeResponse(page([])) for _ in range(2))

    result = await CnMemeClient().lookup("哈哈哈哈")

    assert result["searched_queries"] == ["哈哈哈哈", "哈哈"]
    assert result["matched_query"] is None
    assert len(http.calls) == 2


@pytest.mark.parametrize("max_results", [1, 3])
async def test_supplement_details_are_limited_and_duplicate_slugs_are_read_once(
    http: FakeHTTP, max_results: int
) -> None:
    http.responses.extend(
        [
            FakeResponse(page([])),
            FakeResponse(page([meme("鸡你太美"), meme("鸡你太美"), meme("鸡拟很太美")])),
            FakeResponse(detail("鸡你太美")),
        ]
    )
    if max_results > 1:
        http.responses.append(FakeResponse(detail("鸡拟很太美")))

    result = await CnMemeClient(max_results=max_results).lookup("鸡拟太美")

    detail_urls = [call[0] for call in http.calls if call[0] != SEARCH_URL]
    assert len(detail_urls) == len(set(detail_urls)) == len(result["items"])
    assert len(result["items"]) == min(max_results, 2)
    assert all(item["match_type"] == "approximate" for item in result["items"])
    assert result["searched_queries"] == ["鸡拟太美", "鸡拟"]


@pytest.mark.parametrize("status", [429, 503])
async def test_supplement_error_stops_expansion_and_is_not_cached(http: FakeHTTP, status: int) -> None:
    http.responses.extend(
        [
            FakeResponse(page([])),
            FakeResponse(status=status, headers={"Retry-After": "30"}),
            FakeResponse(page([meme("鸡拟太美")])),
            FakeResponse(detail("鸡拟太美")),
        ]
    )
    client = CnMemeClient()

    with pytest.raises(CnMemeAPIError) as caught:
        await client.lookup("鸡拟太美")
    assert caught.value.status == status
    assert caught.value.retry_after == "30"
    assert not client._cache
    assert [call[1]["params"]["q"] for call in http.calls] == ["鸡拟太美", "鸡拟"]

    assert (await client.lookup("鸡拟太美"))["searched_queries"] == ["鸡拟太美"]
    assert len(http.calls) == 4


async def test_total_timeout_covers_additional_searches(http: FakeHTTP) -> None:
    http.responses.extend([FakeResponse(page([])), FakeResponse(page([]), gate=asyncio.Event())])
    client = CnMemeClient(timeout_seconds=0.01)

    with pytest.raises(asyncio.TimeoutError):
        await client.lookup("鸡拟太美")

    assert len(http.calls) == 2
    assert not client._cache
    assert http.active_sessions == 0 and http.closed_sessions == 1


async def test_cached_results_are_copies_and_expire(http: FakeHTTP, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: clock[0])
    http.responses.extend([FakeResponse(page([meme()])), FakeResponse(detail()), FakeResponse(page([]))])
    client = CnMemeClient(cache_ttl_seconds=60)

    first = await client.lookup("测试梗")
    first["items"][0]["content"] = "调用方修改"
    second = await client.lookup("测试梗")
    assert second["items"][0]["content"] == "完整释义"
    second["items"].clear()
    assert (await client.lookup("测试梗"))["found"] is True
    assert len(http.calls) == 2

    clock[0] = 160.0
    assert (await client.lookup("测试梗"))["found"] is False
    assert len(http.calls) == 3


async def test_cache_can_be_disabled_and_has_bounded_lru_size(http: FakeHTTP, monkeypatch: pytest.MonkeyPatch) -> None:
    http.responses.extend(FakeResponse(page([])) for _ in range(6))
    uncached = CnMemeClient(cache_ttl_seconds=0)
    await uncached.lookup("同词")
    await uncached.lookup("同词")
    assert len(http.calls) == 2
    assert not uncached._cache

    monkeypatch.setattr(client_module, "CACHE_MAX_ENTRIES", 2)
    client = CnMemeClient()
    await client.lookup("甲")
    await client.lookup("乙")
    await client.lookup("甲")
    await client.lookup("丙")
    await client.lookup("乙")
    assert list(client._cache) == ["丙", "乙"]
    assert len(http.calls) == 6


async def test_withdrawn_detail_is_not_replaced_with_stale_summary(http: FakeHTTP) -> None:
    http.responses.extend([FakeResponse(page([meme()])), FakeResponse(status=404)])

    result = await CnMemeClient().lookup("测试梗")

    assert result["success"] is True
    assert result["found"] is False
    assert result["items"] == []
    assert result["unavailable_count"] == 1


@pytest.mark.parametrize("status", [429, 503])
async def test_http_failures_remain_errors_and_are_not_cached(http: FakeHTTP, status: int) -> None:
    http.responses.extend([FakeResponse(status=status, headers={"Retry-After": "30"}), FakeResponse(page([]))])
    client = CnMemeClient()

    with pytest.raises(CnMemeAPIError) as caught:
        await client.lookup("测试梗")
    assert caught.value.status == status
    assert caught.value.retry_after == "30"
    assert not client._cache

    assert (await client.lookup("测试梗"))["success"] is True
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(error=ValueError("private raw response")),
        FakeResponse({"schema_version": 2, "items": [], "has_more": False}),
    ],
)
async def test_invalid_response_is_not_treated_as_no_match(http: FakeHTTP, response: FakeResponse) -> None:
    http.responses.append(response)
    with pytest.raises(CnMemeAPIError) as caught:
        await CnMemeClient().lookup("测试梗")
    assert "private raw response" not in str(caught.value)


async def test_total_timeout_closes_session_and_does_not_cache(http: FakeHTTP) -> None:
    http.responses.append(FakeResponse(page([]), gate=asyncio.Event()))
    client = CnMemeClient(timeout_seconds=0.01)

    with pytest.raises(asyncio.TimeoutError):
        await client.lookup("测试梗")

    assert not client._cache
    assert http.active_sessions == 0 and http.closed_sessions == 1


async def test_full_capacity_rejects_uncached_queries_without_waiting(http: FakeHTTP) -> None:
    client = CnMemeClient(timeout_seconds=0.01)
    await client._slots.acquire()
    await client._slots.acquire()
    try:
        with pytest.raises(CnMemeAPIError) as caught:
            await client.lookup("测试梗")
        assert caught.value.status == 429
    finally:
        client._slots.release()
        client._slots.release()
    assert not http.calls and not http.session_settings


async def test_at_most_two_queries_are_active(http: FakeHTTP) -> None:
    gate = asyncio.Event()
    http.responses.extend(FakeResponse(page([]), gate=gate) for _ in range(2))
    client = CnMemeClient()
    tasks = [asyncio.create_task(client.lookup(f"词语{index}")) for index in range(2)]
    try:
        await asyncio.wait_for(http.two_started.wait(), timeout=1)
        rejected = await asyncio.wait_for(
            asyncio.gather(*(client.lookup(f"其他词{index}") for index in range(20)), return_exceptions=True),
            timeout=1,
        )
        assert all(isinstance(error, CnMemeAPIError) and error.status == 429 for error in rejected)
        assert len(http.calls) == 2
        gate.set()
        results = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert all(result["success"] for result in results)
    assert http.max_active_sessions == 2
    assert http.active_sessions == 0


@pytest.mark.parametrize("query", [" ", "词" * 81])
async def test_invalid_query_never_creates_session(http: FakeHTTP, query: str) -> None:
    with pytest.raises(ValueError):
        await CnMemeClient().lookup(query)
    assert not http.calls and not http.session_settings
