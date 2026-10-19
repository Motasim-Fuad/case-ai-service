"""OpenAI adapters tested with stub clients (no network)."""
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.core.errors import LLMConfigError, LLMOutputError, LLMRefusalError
from app.models.extraction import FinalSummary, RagAnswer, WindowExtraction
from app.services.embeddings import HashEmbedder, OpenAIEmbedder
from app.services.llm import OpenAILLM


def _completion(parsed=None, refusal=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed, refusal=refusal))])


class StubClient:
    def __init__(self, outcomes):
        self.outcomes, self.kwargs = list(outcomes), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))
        self.embeddings = SimpleNamespace(create=self._embed)

    def _parse(self, **kw):
        self.kwargs.append(kw)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def _embed(self, **kw):
        self.kwargs.append(kw)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _req():
    return httpx.Request("POST", "https://api.openai.com/v1/x")


def test_passes_schema_and_returns_parsed(settings):
    obj = FinalSummary(document_type="other", summary="ok")
    client = StubClient([_completion(parsed=obj)])
    out = OpenAILLM(settings, client).parse(system="s", user="u", schema=FinalSummary, history=[{"role": "user", "content": "h"}])
    kw = client.kwargs[0]
    assert out is obj and kw["response_format"] is FinalSummary and "temperature" not in kw
    assert [m["role"] for m in kw["messages"]] == ["system", "user", "user"]
    assert kw["model"] == settings.openai_extraction_model


def test_refusal_and_empty_output(settings):
    with pytest.raises(LLMRefusalError):
        OpenAILLM(settings, StubClient([_completion(refusal="no")])).parse(system="s", user="u", schema=FinalSummary)
    with pytest.raises(LLMOutputError):
        OpenAILLM(settings, StubClient([_completion(parsed=None)])).parse(system="s", user="u", schema=FinalSummary)


def test_auth_error_is_permanent_config_error(settings):
    err = openai.AuthenticationError("bad key", response=httpx.Response(401, request=_req()), body=None)
    with pytest.raises(LLMConfigError):
        OpenAILLM(settings, StubClient([err])).parse(system="s", user="u", schema=FinalSummary)


def test_rate_limit_is_retried(settings, monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    rl = openai.RateLimitError("slow down", response=httpx.Response(429, request=_req()), body=None)
    client = StubClient([rl, rl, _completion(parsed=FinalSummary(document_type="other", summary="ok"))])
    assert OpenAILLM(settings, client).parse(system="s", user="u", schema=FinalSummary).summary == "ok"
    assert len(client.kwargs) == 3


def test_temperature_only_when_configured(settings):
    s = settings.model_copy(update={"llm_temperature": 0.0})
    client = StubClient([_completion(parsed=FinalSummary(document_type="other", summary="ok"))])
    OpenAILLM(s, client).parse(system="s", user="u", schema=FinalSummary)
    assert client.kwargs[0]["temperature"] == 0.0


def test_embedder_batches_and_orders(settings):
    s = settings.model_copy(update={"embedding_batch_size": 2, "embedding_concurrency": 1})
    mk = lambda n: SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[float(i)] * 3) for i in reversed(range(n))])
    client = StubClient([mk(2), mk(1)])
    vecs = OpenAIEmbedder(s, client).embed(["a", "b", "c"])
    assert len(vecs) == 3 and vecs[0] == [0.0] * 3 and vecs[1] == [1.0] * 3  # re-ordered by index
    assert len(client.kwargs) == 2 and client.kwargs[0]["model"] == s.openai_embedding_model


def test_hash_embedder_is_deterministic_and_lexical():
    e = HashEmbedder(64)
    a, b, c = e.embed(["repair estimate cost", "repair estimate cost", "gardening soil"])
    assert a == b and sum(x * y for x, y in zip(a, a)) == pytest.approx(1.0)
    assert sum(x * y for x, y in zip(a, c)) < 0.5


@pytest.mark.parametrize("model", [WindowExtraction, FinalSummary, RagAnswer])
def test_schemas_are_openai_strict_compatible(model):
    """The SDK helper raises if a schema uses anything strict Structured Outputs can't handle (e.g. dict)."""
    from openai.lib._parsing._completions import type_to_response_format_param

    fmt = type_to_response_format_param(model)
    assert fmt["json_schema"]["strict"] is True
    json.dumps(fmt)
