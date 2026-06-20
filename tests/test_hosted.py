"""Hosted (production) integration paths, exercised with mock SDKs.

Real beneficiaries run Leptin with hosted embeddings/LLM, so the integration
code must be correct even though we can't call the APIs in tests. We inject fake
``openai`` / ``anthropic`` / ``voyageai`` modules and assert the call shapes.
"""

from __future__ import annotations

import sys
import types

import pytest

from leptin.embeddings import HostedEmbedder, make_embedder
from leptin.llm import HostedMerger, make_merger


# ---------------------------------------------------------------- selection
def test_make_embedder_selects_hosted_for_known_models():
    assert isinstance(make_embedder("text-embedding-3-small"), HostedEmbedder)
    assert isinstance(make_embedder("voyage-3"), HostedEmbedder)


def test_make_merger_selects_hosted_for_real_models():
    assert isinstance(make_merger("claude-haiku-4-5"), HostedMerger)
    assert isinstance(make_merger("gpt-4o-mini"), HostedMerger)
    assert make_merger("heuristic").name == "heuristic"


# ---------------------------------------------------------------- openai
def _install_fake_openai(monkeypatch, capture):
    mod = types.ModuleType("openai")

    class _Embeddings:
        def create(self, model, input):
            capture["embed"] = {"model": model, "input": input}
            item = types.SimpleNamespace(embedding=[0.1, 0.2, 0.3])
            return types.SimpleNamespace(data=[item])

    class _ChatCompletions:
        def create(self, model, messages, max_tokens):
            capture["chat"] = {"model": model, "messages": messages}
            msg = types.SimpleNamespace(message=types.SimpleNamespace(content="MERGE\nfused canonical"))
            return types.SimpleNamespace(choices=[msg])

    class OpenAI:
        def __init__(self, *a, **k):
            self.embeddings = _Embeddings()
            self.chat = types.SimpleNamespace(completions=_ChatCompletions())

    mod.OpenAI = OpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)


def test_hosted_openai_embedding(monkeypatch):
    capture = {}
    _install_fake_openai(monkeypatch, capture)
    emb = HostedEmbedder("text-embedding-3-small")
    vec = emb.embed("hello world")
    assert vec == [0.1, 0.2, 0.3]
    assert capture["embed"]["model"] == "text-embedding-3-small"
    assert capture["embed"]["input"] == "hello world"


def test_hosted_openai_merge(monkeypatch):
    capture = {}
    _install_fake_openai(monkeypatch, capture)
    merger = make_merger("gpt-4o-mini")
    result = merger.decide("old fact", "new fact", 0.9)
    assert result.action == "merge"
    assert "fused canonical" in result.content
    assert capture["chat"]["model"] == "gpt-4o-mini"


# ---------------------------------------------------------------- anthropic
def test_hosted_anthropic_merge(monkeypatch):
    capture = {}
    mod = types.ModuleType("anthropic")

    class _Messages:
        def create(self, model, max_tokens, messages):
            capture["model"] = model
            block = types.SimpleNamespace(type="text", text="SUPERSEDE\nthe newer fact")
            return types.SimpleNamespace(content=[block])

    class Anthropic:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    mod.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)

    merger = HostedMerger("claude-haiku-4-5")
    result = merger.decide("old", "new", 0.4)
    assert result.action == "supersede"
    assert capture["model"] == "claude-haiku-4-5"


# ---------------------------------------------------------------- voyage
def test_hosted_voyage_embedding(monkeypatch):
    mod = types.ModuleType("voyageai")

    class Client:
        def embed(self, texts, model):
            return types.SimpleNamespace(embeddings=[[0.5, 0.6]])

    mod.Client = Client
    monkeypatch.setitem(sys.modules, "voyageai", mod)

    emb = HostedEmbedder("voyage-3")
    assert emb.embed("x") == [0.5, 0.6]


# -------------------------------------------------- graceful degradation
def test_hosted_embedder_retries_transient_then_succeeds():
    """A transient embedding failure is retried, not permanently downgraded."""
    from leptin.config import Config
    from leptin.engine import DietEngine
    from leptin.storage import Store

    class _Flaky:
        name = "flaky-hosted"
        dim = 3
        calls = 0

        def embed(self, text):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("transient 429")
            return [0.1, 0.2, 0.3]

    store = Store(":memory:")
    eng = DietEngine(store, Config(embedding_model="text-embedding-3-small"),
                     embedder=_Flaky())
    eng._retry_backoff = 0.0  # no sleep in tests
    vec = eng._embed("hello")
    assert vec == [0.1, 0.2, 0.3]
    assert eng._offline is False  # did NOT downgrade — retry recovered it
    store.close()


def test_hosted_embedder_downgrades_after_exhausting_retries():
    from leptin.config import Config
    from leptin.engine import DietEngine
    from leptin.storage import Store

    class _Dead:
        name = "dead-hosted"
        dim = 3

        def embed(self, text):
            raise ConnectionError("down")

    store = Store(":memory:")
    eng = DietEngine(store, Config(embedding_model="text-embedding-3-small"),
                     embedder=_Dead())
    eng._retry_backoff = 0.0
    vec = eng._embed("hello")  # retries, then falls back to local for this call
    # v1.3: a TRANSIENT outage degrades non-permanently — local for now + a
    # cooldown, then retry hosted — instead of pinning the store to local forever.
    assert vec  # still got a (local) vector; never raised
    assert eng._last_local is True
    assert eng._hosted_cooldown_until > 0
    assert eng._offline is False  # NOT permanently pinned
    store.close()


def test_hosted_merger_unavailable_degrades_on_near_duplicate(monkeypatch):
    """PRD 8.1(d): a hosted LLM merger that's unreachable must NOT crash
    remember() when a near-duplicate hits the merge path — it falls back to the
    heuristic merger (regression for the audit-found ConnectionError)."""
    from leptin.config import Config
    from leptin.engine import DietEngine
    from leptin.storage import Store

    class _RaisingMerger:
        name = "hosted"

        def decide(self, older, newer, similarity):
            raise ConnectionError("LLM API unreachable")

    store = Store(":memory:")
    engine = DietEngine(store, Config(), merger=_RaisingMerger())
    engine.remember("The deploy target is Fly.io.", subject="infra")
    # Exact duplicate → hits the merge/supersede path → would call merger.decide.
    r = engine.remember("The deploy target is Fly.io.", subject="infra")
    assert r["action"] in ("created", "merged", "superseded")  # no exception
    assert engine._merger_offline is True  # degraded persistently
    store.close()


def test_hosted_unavailable_degrades_to_local(monkeypatch):
    """If a hosted model is configured but the SDK/key is missing, the engine
    must fall back to local embeddings rather than crash (PRD edge case)."""
    from leptin.config import Config
    from leptin.engine import DietEngine
    from leptin.storage import Store

    # Ensure the import fails inside HostedEmbedder.embed.
    monkeypatch.setitem(sys.modules, "openai", None)
    store = Store(":memory:")
    engine = DietEngine(store, Config(embedding_model="text-embedding-3-small"))
    r = engine.remember("Resilient fact about fallbacks.", subject="x")
    assert r["action"] in ("created", "merged")
    res = engine.recall("resilient fallback")
    assert isinstance(res["memories"], list)
    store.close()


# ------------------------------------ engine-level hosted integration tests


def _install_working_openai(monkeypatch, dim=8):
    """A fake `openai` whose embeddings vary with the text (real cosine signal)."""
    mod = types.ModuleType("openai")

    def _vec(text):
        v = [0.0] * dim
        for w in text.lower().split():
            v[hash(w) % dim] += 1.0
        return v

    class _Embeddings:
        def create(self, model, input):
            return types.SimpleNamespace(
                data=[types.SimpleNamespace(embedding=_vec(input))]
            )

    class _ChatCompletions:
        def create(self, model, messages, max_tokens):
            msg = types.SimpleNamespace(
                message=types.SimpleNamespace(content="MERGE\nfused canonical fact")
            )
            return types.SimpleNamespace(choices=[msg])

    class OpenAI:
        def __init__(self, *a, **k):
            self.embeddings = _Embeddings()
            self.chat = types.SimpleNamespace(completions=_ChatCompletions())

    mod.OpenAI = OpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)


def test_engine_runs_full_hosted_pipeline(monkeypatch):
    """remember -> recall must work end-to-end with a hosted embedder + merger
    that actually succeed (no degradation)."""
    from leptin.config import Config
    from leptin.embeddings import HostedEmbedder
    from leptin.engine import DietEngine
    from leptin.storage import Store

    _install_working_openai(monkeypatch)
    store = Store(":memory:")
    engine = DietEngine(
        store,
        Config(embedding_model="text-embedding-3-small", llm_model="gpt-4o-mini"),
    )
    # Hosted embedder stays active (SDK works) — no fallback to local.
    engine.remember("Alice leads the payments team.", subject="people")
    assert isinstance(engine.embedder, HostedEmbedder)
    assert engine._offline is False
    res = engine.recall("who leads payments")
    assert any("payments" in m["content"].lower() for m in res["memories"])
    store.close()


def test_dimension_mismatch_offline_to_hosted_still_recalls(monkeypatch):
    """A store written offline (256-dim) then queried hosted (different dim)
    must not crash; similarity falls back to keyword overlap and still recalls."""
    from leptin.config import Config
    from leptin.engine import DietEngine
    from leptin.storage import Store

    store = Store(":memory:")
    engine = DietEngine(store, Config(embedding_model="local-hash", embedding_dim=256))
    engine.remember("The deploy script lives in scripts/deploy.sh", subject="ops")
    assert all(len(m["embedding"]) == 256 for m in store.list_memories("active"))

    # Switch to a hosted embedder returning a different dimensionality.
    class _HE:
        name = "text-embedding-3-small"
        dim = 1536

        def embed(self, text):
            return [0.01] * 1536

    engine.embedder = _HE()
    engine._offline = False
    engine._tok_model = "heuristic"  # keep ledger math deterministic for the assert
    res = engine.recall("where is the deploy script")
    assert res["memories"], "mixed-dimension store should still recall via keyword"
    store.close()


def test_hosted_dedup_still_works_after_sdk_missing_degradation(monkeypatch):
    """Hosted embedding configured but SDK missing: first remember degrades to
    local, and a subsequent near-duplicate must still merge (not silently dupe)."""
    from leptin.config import Config
    from leptin.engine import DietEngine
    from leptin.storage import Store

    monkeypatch.setitem(sys.modules, "openai", None)  # import fails -> degrade
    store = Store(":memory:")
    engine = DietEngine(
        store,
        Config(embedding_model="text-embedding-3-small", llm_model="heuristic"),
    )
    engine.remember("User prefers dark mode in the editor.", subject="prefs")
    assert engine._offline is True  # degraded on first embed
    r2 = engine.remember("The user prefers dark mode in their editor.", subject="prefs")
    assert r2["action"] in ("merged", "superseded")
    assert store.count_memories("active") == 1
    store.close()


def test_hosted_merge_literal_backslash_n_is_parsed(monkeypatch):
    """REGRESSION: the merge prompt instructs the model to reply 'MERGE\\n<fact>'
    with a *literal* backslash-n, but parsing splits on a real newline. A model
    that echoes the literal form must not silently drop the fused content."""
    capture = {}
    mod = types.ModuleType("openai")

    class _ChatCompletions:
        def create(self, model, messages, max_tokens):
            capture["sent"] = messages[0]["content"]
            # Model follows the prompt's literal "\n" instruction verbatim.
            content = "MERGE\\n fused canonical fact"  # literal backslash-n
            msg = types.SimpleNamespace(message=types.SimpleNamespace(content=content))
            return types.SimpleNamespace(choices=[msg])

    class OpenAI:
        def __init__(self, *a, **k):
            self.chat = types.SimpleNamespace(completions=_ChatCompletions())

    mod.OpenAI = OpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)

    merger = HostedMerger("gpt-4o-mini")
    result = merger.decide("old fact", "new fact", 0.9)
    # The fused content must survive parsing regardless of literal-vs-real newline.
    assert "fused canonical fact" in result.content, (
        "literal '\\n' in the model reply dropped the fused content; "
        "the prompt and the parser disagree on the separator"
    )
