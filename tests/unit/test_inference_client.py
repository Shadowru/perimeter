import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_openai_server import FakeOpenAIServer, Scripted
from perimeter_inference.client import InferenceClient, InferenceError

MSGS = [{"role": "user", "content": "привет"}]


def test_chat_non_streaming():
    with FakeOpenAIServer([Scripted(content="Здравствуйте! Чем помочь?")]) as srv:
        client = InferenceClient(srv.base_url)
        result = client.chat(MSGS)
        assert result.content == "Здравствуйте! Чем помочь?"
        assert result.finish_reason == "stop"
        assert srv.requests[0]["messages"] == MSGS


def test_chat_streaming_accumulates():
    with FakeOpenAIServer([Scripted(content="раз два три")]) as srv:
        client = InferenceClient(srv.base_url)
        chunks = list(client.chat_stream(MSGS))
        text = "".join(c.content for c in chunks)
        assert text == "раз два три"
        assert chunks[-2].finish_reason == "stop" or chunks[-1].finish_reason == "stop"
        assert any(c.usage for c in chunks)


def test_tool_calls_roundtrip():
    tc = {"name": "find_document", "arguments": {"type": "РеализацияТоваровУслуг"}}
    with FakeOpenAIServer([Scripted(tool_calls=[tc])]) as srv:
        client = InferenceClient(srv.base_url)
        result = client.chat(MSGS, tools=[{"type": "function", "function": {"name": "find_document"}}])
        assert result.tool_calls[0]["function"]["name"] == "find_document"
        assert "РеализацияТоваровУслуг" in result.tool_calls[0]["function"]["arguments"]
        assert result.finish_reason == "tool_calls"


def test_health():
    with FakeOpenAIServer([]) as srv:
        assert InferenceClient(srv.base_url).health()["status"] == "ok"


def test_script_exhausted_raises():
    with FakeOpenAIServer([]) as srv:
        client = InferenceClient(srv.base_url)
        with pytest.raises(InferenceError):
            client.chat(MSGS)


def test_connection_refused_raises():
    client = InferenceClient("http://127.0.0.1:1", timeout_s=2)
    with pytest.raises(InferenceError):
        client.chat(MSGS)
