"""Unit tests for the Signal bridge's pure logic and the intent dispatcher.

These cover the classifier (is_url / is_short_topic), envelope extraction, the
referential-word detector, and the dispatcher's templating + matching. One mocked
end-to-end intent flow locks in run_intent's step pipeline.
"""

import json
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


# --- extract_attachments ----------------------------------------------------

def test_extract_attachments_data_message():
    env = {"envelope": {"source": "+3581", "dataMessage": {
        "attachments": [{"id": "a1", "contentType": "image/jpeg"}]}}}
    assert [a["id"] for a in app.extract_attachments(env)] == ["a1"]


def test_extract_attachments_sync_message():
    # Note-to-Self photos arrive under syncMessage.sentMessage, not dataMessage.
    env = {"envelope": {"source": "+3581", "syncMessage": {"sentMessage": {
        "attachments": [{"id": "s1", "contentType": "image/png"}]}}}}
    assert [a["id"] for a in app.extract_attachments(env)] == ["s1"]


def test_extract_attachments_combines_both_lists():
    env = {"envelope": {
        "dataMessage": {"attachments": [{"id": "d", "contentType": "image/jpeg"}]},
        "syncMessage": {"sentMessage": {"attachments": [
            {"id": "s", "contentType": "image/heic"}]}}}}
    assert sorted(a["id"] for a in app.extract_attachments(env)) == ["d", "s"]


def test_extract_attachments_filters_non_image():
    env = {"envelope": {"dataMessage": {"attachments": [
        {"id": "img", "contentType": "image/jpeg"},
        {"id": "doc", "contentType": "application/pdf"},
        {"id": "none"},
    ]}}}
    assert [a["id"] for a in app.extract_attachments(env)] == ["img"]


def test_extract_attachments_empty():
    env = {"envelope": {"dataMessage": {"message": "hi"}}}
    assert app.extract_attachments(env) == []


# --- download_attachment ----------------------------------------------------

@respx.mock
async def test_download_attachment_writes_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SIGNAL_API_URL", "http://sig.test")
    respx.get("http://sig.test/v1/attachments/abc").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8jpegdata"))
    async with httpx.AsyncClient() as client:
        path = await app.download_attachment(
            client, {"id": "abc", "contentType": "image/jpeg"}, tmp_path)
    assert path is not None
    assert path.parent == tmp_path
    assert path.suffix == ".jpg"
    assert path.name.startswith("takuu_")
    assert path.read_bytes() == b"\xff\xd8jpegdata"


@respx.mock
async def test_download_attachment_ext_from_contenttype(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SIGNAL_API_URL", "http://sig.test")
    respx.get("http://sig.test/v1/attachments/p").mock(
        return_value=httpx.Response(200, content=b"png"))
    async with httpx.AsyncClient() as client:
        path = await app.download_attachment(
            client, {"id": "p", "contentType": "image/png"}, tmp_path)
    assert path.suffix == ".png"


@respx.mock
async def test_download_attachment_unsupported_type_rejected(monkeypatch, tmp_path):
    # image/svg+xml passes the startswith("image/") filter but is script-bearing;
    # it must be rejected, never saved (and not even downloaded).
    monkeypatch.setattr(app, "SIGNAL_API_URL", "http://sig.test")
    async with httpx.AsyncClient() as client:
        path = await app.download_attachment(
            client, {"id": "svg", "contentType": "image/svg+xml"}, tmp_path)
    assert path is None
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_download_attachment_collision_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SIGNAL_API_URL", "http://sig.test")
    # Freeze the basename so both saves collide deterministically, exercising
    # the exclusive-create suffix loop regardless of wall-clock timing.
    monkeypatch.setattr(app, "_attach_basename", lambda: "takuu_FIXED")
    respx.get("http://sig.test/v1/attachments/x").mock(
        return_value=httpx.Response(200, content=b"data"))
    att = {"id": "x", "contentType": "image/jpeg"}
    async with httpx.AsyncClient() as client:
        p1 = await app.download_attachment(client, att, tmp_path)
        p2 = await app.download_attachment(client, att, tmp_path)
    assert p1.name == "takuu_FIXED.jpg"
    assert p2.name == "takuu_FIXED_1.jpg"  # second must not clobber the first
    assert p1.read_bytes() == b"data" and p2.read_bytes() == b"data"


async def test_download_attachment_zero_id_is_valid(tmp_path, monkeypatch):
    # 0 is a falsy-but-valid id; it must not be treated as "no id".
    monkeypatch.setattr(app, "SIGNAL_API_URL", "http://sig.test")
    monkeypatch.setattr(app, "_attach_basename", lambda: "takuu_ZERO")
    with respx.mock:
        respx.get("http://sig.test/v1/attachments/0").mock(
            return_value=httpx.Response(200, content=b"z"))
        async with httpx.AsyncClient() as client:
            path = await app.download_attachment(
                client, {"id": 0, "contentType": "image/jpeg"}, tmp_path)
    assert path is not None and path.read_bytes() == b"z"


@respx.mock
async def test_download_attachment_http_error_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SIGNAL_API_URL", "http://sig.test")
    respx.get("http://sig.test/v1/attachments/bad").mock(
        return_value=httpx.Response(500, text="boom"))
    async with httpx.AsyncClient() as client:
        path = await app.download_attachment(
            client, {"id": "bad", "contentType": "image/jpeg"}, tmp_path)
    assert path is None
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_download_attachment_empty_content_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SIGNAL_API_URL", "http://sig.test")
    respx.get("http://sig.test/v1/attachments/e").mock(
        return_value=httpx.Response(200, content=b""))
    async with httpx.AsyncClient() as client:
        path = await app.download_attachment(
            client, {"id": "e", "contentType": "image/jpeg"}, tmp_path)
    assert path is None


async def test_download_attachment_no_id_returns_none(tmp_path):
    async with httpx.AsyncClient() as client:
        path = await app.download_attachment(
            client, {"contentType": "image/jpeg"}, tmp_path)
    assert path is None


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


# --- load_history_context: last_result_file path safety ---------------------

def _setup_inbox(monkeypatch, tmp_path, result_line):
    """Point app at a temp vault, seed one history entry, return the inbox dir."""
    inbox = tmp_path / "Signal inbox"
    inbox.mkdir()
    entry = {"ts": "2026-06-29T00:00:00", "msg": "save it",
             "mode": "note", "result": result_line}
    (inbox / ".bridge-history.jsonl").write_text(
        json.dumps(entry) + "\n", encoding="utf-8")
    monkeypatch.setattr(app, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(app, "SIGNAL_INBOX", "Signal inbox")
    monkeypatch.setattr(app, "_HISTORY_PATH", None)
    return inbox


def test_load_history_context_reads_bare_filename(monkeypatch, tmp_path):
    """Positive control: a bare filename is read for a referential message."""
    inbox = _setup_inbox(monkeypatch, tmp_path, "OK: note.md — saved")
    (inbox / "note.md").write_text("BENIGN_NOTE_BODY", encoding="utf-8")
    ctx = app.load_history_context("tell me more about that")
    assert "BENIGN_NOTE_BODY" in ctx


@pytest.mark.parametrize("bad_name", [
    "../secret.txt",          # parent-dir traversal
    "..\\secret.txt",         # backslash traversal (Windows)
    "sub/note.md",            # forward-slash separator
    "sub\\note.md",           # backslash separator
])
def test_load_history_context_rejects_path_traversal(monkeypatch, tmp_path, bad_name):
    """A non-bare filename in the OK: line must never be read."""
    _setup_inbox(monkeypatch, tmp_path, f"OK: {bad_name} — saved")
    # Plant a secret one level above the inbox; `../secret.txt` would resolve here.
    (tmp_path / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")
    ctx = app.load_history_context("tell me more about that")
    assert "TOP_SECRET" not in ctx
    assert "Content of most recent note" not in ctx
