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
            FakeResponse(page([meme("仅正文提及"), meme(slug, title="目标词")])),
            FakeResponse(detail(slug, title="目标词")),
        ]
    )

    result = await CnMemeClient().lookup(" 目标词 ")

    assert result["success"] is True and result["found"] is True
    assert result["query"] == "目标词"
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
    http.responses.append(FakeResponse(page([])))
    client = CnMemeClient()

    result = await client.lookup("未收录词")
    cached = await client.lookup("未收录词")

    assert result == cached
    assert result["success"] is True and result["found"] is False
    assert result["items"] == []
    assert len(http.calls) == 1


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


async def test_total_timeout_also_covers_waiting_for_query_slot(http: FakeHTTP) -> None:
    client = CnMemeClient(timeout_seconds=0.01)
    await client._slots.acquire()
    await client._slots.acquire()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.lookup("测试梗")
    finally:
        client._slots.release()
        client._slots.release()
    assert not http.calls and not http.session_settings


async def test_at_most_two_queries_are_active(http: FakeHTTP) -> None:
    gate = asyncio.Event()
    http.responses.extend(FakeResponse(page([]), gate=gate) for _ in range(4))
    client = CnMemeClient()
    tasks = [asyncio.create_task(client.lookup(f"词语{index}")) for index in range(4)]
    try:
        await asyncio.wait_for(http.two_started.wait(), timeout=1)
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
