"""Unit tests for the Signal bridge's pure logic and the intent dispatcher.

These cover the classifier (is_url / is_short_topic), envelope extraction, the
referential-word detector, and the dispatcher's templating + matching. One mocked
end-to-end intent flow locks in run_intent's step pipeline.
"""

import re

import httpx
import pytest
import respx

import app
import intent_dispatcher as idp


# --- Classifier: is_url -----------------------------------------------------

@pytest.mark.parametrize("msg", [
    "https://example.com",
    "http://example.com/path?q=1",
    "  https://example.com/article  ",  # surrounding whitespace is stripped
    "HTTPS://EXAMPLE.COM",
])
def test_is_url_true(msg):
    assert app.is_url(msg) is True


@pytest.mark.parametrize("msg", [
    "stoicism",
    "check https://example.com out",   # URL not the whole message
    "ftp://example.com",
    "example.com",                     # no scheme
    "",
])
def test_is_url_false(msg):
    assert app.is_url(msg) is False


# --- Classifier: is_short_topic ---------------------------------------------

@pytest.mark.parametrize("msg", [
    "stoicism",
    "roman empire",
    "best mechanical keyboard",        # 3 words
    "what is entropy",                 # 3 words, no terminal punctuation
])
def test_is_short_topic_true(msg):
    assert app.is_short_topic(msg) is True


@pytest.mark.parametrize("msg", [
    "summarise the key points of the EU AI Act",  # > 4 words
    "what is entropy?",                            # sentence punctuation
    "line one\nline two",                          # newline
    "a " * 40,                                     # > 60 chars
])
def test_is_short_topic_false(msg):
    assert app.is_short_topic(msg) is False


def test_short_topic_respects_word_cap(monkeypatch):
    # The env var is a word count, not a token count.
    monkeypatch.setattr(app, "SHORT_TOPIC_MAX_TOKENS", 2)
    assert app.is_short_topic("two words") is True
    assert app.is_short_topic("now three words") is False


# --- extract_message --------------------------------------------------------

def test_extract_message_data_message():
    env = {"envelope": {"source": "+3581", "dataMessage": {"message": "hello"}}}
    assert app.extract_message(env) == ("+3581", "hello")


def test_extract_message_prefers_source_number():
    env = {"envelope": {"sourceNumber": "+3582", "source": "uuid",
                        "dataMessage": {"message": "hi"}}}
    assert app.extract_message(env) == ("+3582", "hi")


def test_extract_message_note_to_self_sync():
    env = {"envelope": {"source": "+3581",
                        "syncMessage": {"sentMessage": {"message": "note"}}}}
    assert app.extract_message(env) == ("+3581", "note")


def test_extract_message_empty_sync_returns_none():
    env = {"envelope": {"source": "+3581", "syncMessage": {"sentMessage": {}}}}
    assert app.extract_message(env) is None


def test_extract_message_no_text_returns_none():
    env = {"envelope": {"source": "+3581", "dataMessage": {}}}
    assert app.extract_message(env) is None


def test_extract_message_no_source_returns_none():
    env = {"envelope": {"dataMessage": {"message": "hello"}}}
    assert app.extract_message(env) is None


# --- _is_referential --------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "tell me more about that",
    "expand on the previous note",
    "lisää tietoa edellisestä",   # Finnish
])
def test_is_referential_true(msg):
    assert app._is_referential(msg) is True


@pytest.mark.parametrize("msg", ["stoicism", "what is the capital of France"])
def test_is_referential_false(msg):
    assert app._is_referential(msg) is False


# --- Dispatcher: _envsub ----------------------------------------------------

def test_envsub_replaces_known(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc123")
    assert idp._envsub("Bearer ${MY_TOKEN}") == "Bearer abc123"


def test_envsub_unknown_becomes_empty(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert idp._envsub("x=${NOPE}") == "x="


# --- Dispatcher: _resolve ---------------------------------------------------

def test_resolve_dotted_and_indexed():
    ctx = {"lookup": {"title": "Dune", "cast": [{"name": "Paul"}]}}
    assert idp._resolve("lookup.title", ctx) == "Dune"
    assert idp._resolve("lookup.cast[0].name", ctx) == "Paul"


def test_resolve_missing_is_none():
    assert idp._resolve("a.b.c", {"a": {}}) is None


def test_resolve_out_of_range_index_is_none():
    assert idp._resolve("[5]", [1, 2]) is None


# --- Dispatcher: _render ----------------------------------------------------

def test_render_whole_placeholder_preserves_type():
    ctx = {"qp": {"id": 7}}
    # A string that is exactly one placeholder returns the raw value (int, not str).
    assert idp._render("{qp.id}", ctx) == 7
    assert isinstance(idp._render("{qp.id}", ctx), int)


def test_render_mixed_text_is_string():
    ctx = {"m": {"title": "Dune", "year": 2021}}
    assert idp._render("{m.title} ({m.year})", ctx) == "Dune (2021)"


def test_render_does_not_touch_env_tokens():
    # ${VAR} must survive _render so _envsub can handle it later.
    ctx = {"value": "x"}
    assert idp._render("${RADARR_URL}/api/{value}", ctx) == "${RADARR_URL}/api/x"


def test_render_recurses_into_dict_and_list():
    ctx = {"a": 1, "b": "two"}
    out = idp._render({"x": "{a}", "y": ["{b}", "lit"]}, ctx)
    assert out == {"x": 1, "y": ["two", "lit"]}


# --- Dispatcher: match_intent -----------------------------------------------

def _compile(intent: dict) -> dict:
    intent["_re"] = re.compile(intent["match"], re.IGNORECASE)
    return intent


def test_match_intent_named_group_and_value(monkeypatch):
    intent = _compile({"name": "radarr", "match": r"^add (?P<title>.+) to radarr$"})
    monkeypatch.setattr(idp, "_INTENTS", [intent])
    matched = idp.match_intent("add Dune to Radarr")
    assert matched is not None
    got_intent, groups = matched
    assert got_intent["name"] == "radarr"
    assert groups["title"] == "Dune"
    assert groups["value"] == "Dune"   # first named group exposed as `value`


def test_match_intent_no_match(monkeypatch):
    intent = _compile({"name": "x", "match": r"^announce (?P<value>.+)$"})
    monkeypatch.setattr(idp, "_INTENTS", [intent])
    assert idp.match_intent("hello there") is None


# --- Dispatcher: run_intent (mocked HTTP) -----------------------------------

@respx.mock
async def test_run_intent_two_step_flow(monkeypatch):
    monkeypatch.setenv("SVC_URL", "http://svc.test")
    lookup = respx.get("http://svc.test/lookup").mock(
        return_value=httpx.Response(200, json=[{"id": 42, "title": "Dune"}]))
    add = respx.post("http://svc.test/add").mock(
        return_value=httpx.Response(201, json={"ok": True}))

    intent = {
        "name": "add_thing",
        "steps": [
            {"method": "GET", "url": "${SVC_URL}/lookup",
             "params": {"term": "{value}"}, "extract": "[0]", "save_as": "hit",
             "fail_if_empty": "no match for {value}"},
            {"method": "POST", "url": "${SVC_URL}/add",
             "json": {"id": "{hit.id}", "title": "{hit.title}"}},
        ],
        "reply": "OK: added {hit.title} (id {hit.id})",
    }
    reply = await idp.run_intent(intent, {"value": "Dune"})
    assert reply == "OK: added Dune (id 42)"
    assert lookup.called and add.called
    # The int id survived templating into the POST body (42, not "42").
    sent = add.calls.last.request
    assert b'"id":42' in sent.content


@respx.mock
async def test_run_intent_fail_if_empty(monkeypatch):
    monkeypatch.setenv("SVC_URL", "http://svc.test")
    respx.get("http://svc.test/lookup").mock(
        return_value=httpx.Response(200, json=[]))
    intent = {
        "name": "add_thing",
        "steps": [
            {"method": "GET", "url": "${SVC_URL}/lookup", "extract": "[0]",
             "save_as": "hit", "fail_if_empty": "no match for {value}"},
        ],
        "reply": "OK: should not reach",
    }
    reply = await idp.run_intent(intent, {"value": "Nope"})
    assert reply == "FAIL: no match for Nope"


@respx.mock
async def test_run_intent_http_error_status(monkeypatch):
    monkeypatch.setenv("SVC_URL", "http://svc.test")
    respx.get("http://svc.test/lookup").mock(
        return_value=httpx.Response(500, text="boom"))
    intent = {
        "name": "add_thing",
        "steps": [{"method": "GET", "url": "${SVC_URL}/lookup"}],
        "reply": "OK: unreached",
    }
    reply = await idp.run_intent(intent, {"value": "x"})
    assert reply.startswith("FAIL:")
    assert "500" in reply
