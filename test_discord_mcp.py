"""Tests for the Discord bridge.

Two of these matter more than the rest. Mentions are allowlisted rather
than parsed, because the message body is written by a model and an
@everyone reaching a real server is not recoverable. And the webhook URL
never appears in a returned string, because tool output lands in a
model's context and the URL is the whole credential.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import discord_mcp


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Capture the payload instead of posting it."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        captured["headers"] = request.headers
        return FakeResponse()

    monkeypatch.setattr(discord_mcp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(discord_mcp, "WEBHOOK", "https://discord.com/api/webhooks/1/secret")
    return captured


# --- the mention allowlist ------------------------------------------------


def test_say_allows_no_mentions_at_all(sent, monkeypatch):
    monkeypatch.setattr(discord_mcp, "MENTION_ID", "42")
    discord_mcp.discord_say("build finished", source="isaac")
    assert sent["body"]["allowed_mentions"] == {"parse": [], "users": []}


def test_an_everyone_in_the_text_cannot_ping(sent, monkeypatch):
    """The model wrote the body. It does not get to decide who is woken."""
    monkeypatch.setattr(discord_mcp, "MENTION_ID", "42")
    discord_mcp.discord_say("@everyone the build is broken", source="isaac")
    assert sent["body"]["allowed_mentions"]["parse"] == []
    assert sent["body"]["allowed_mentions"]["users"] == []


def test_ask_pings_exactly_one_user(sent, monkeypatch):
    monkeypatch.setattr(discord_mcp, "MENTION_ID", "42")
    discord_mcp.discord_ask("postgres or sqlite?", source="isaac")
    assert sent["body"]["allowed_mentions"]["users"] == ["42"]
    assert sent["body"]["content"].startswith("<@42> ")


def test_ask_without_a_configured_id_still_posts(sent, monkeypatch):
    """No id is a missing convenience, not a reason to swallow the question."""
    monkeypatch.setattr(discord_mcp, "MENTION_ID", "")
    result = discord_mcp.discord_ask("which one?", source="isaac")
    assert result.startswith("sent")
    assert "<@" not in sent["body"]["content"]


# --- the credential never comes back --------------------------------------


def test_an_http_error_does_not_echo_the_url(monkeypatch):
    monkeypatch.setattr(discord_mcp, "WEBHOOK", "https://discord.com/api/webhooks/1/secret")

    def fail(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(discord_mcp.urllib.request, "urlopen", fail)
    result = discord_mcp.discord_say("hello")
    assert "secret" not in result
    assert "404" in result


def test_a_missing_webhook_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(discord_mcp, "WEBHOOK", "")
    assert "not sent" in discord_mcp.discord_say("hello")


# --- the copy-to-phone path -----------------------------------------------


@pytest.fixture
def posts(monkeypatch):
    """Capture every message, since one call can produce several."""
    bodies = []

    def fake_urlopen(request, timeout=None):
        bodies.append(json.loads(request.data.decode()))
        return FakeResponse()

    monkeypatch.setattr(discord_mcp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(discord_mcp, "WEBHOOK", "https://discord.com/api/webhooks/1/secret")
    return bodies


def test_content_is_sent_alone_so_one_press_copies_it(posts):
    """A long-press copies the whole message, so the whole message has to
    be the content and nothing else -- no label, no fence, no marker."""
    discord_mcp.discord_send("psql -h localhost -U isaac")
    assert posts == [posts[0]]
    assert posts[0]["content"] == "psql -h localhost -U isaac"


def test_content_is_not_escaped_or_fenced(posts):
    """Copying a message on mobile yields its raw source, so anything added
    to protect the rendering -- a fence, a backslash before an underscore --
    is pasted into the shell along with the command."""
    command = 'psql postgresql://roomie_bills:CHANGE_ME@localhost/roomie_bills_db -c "select settle_day_of_month from bill_config"'
    discord_mcp.discord_send(command)
    assert posts[0]["content"] == command


def test_the_note_is_its_own_message(posts):
    """An explanation sharing the message is pasted with the command."""
    discord_mcp.discord_send("SELECT 1;", note="run this on core1")
    assert len(posts) == 2
    assert "run this on core1" in posts[0]["content"]
    assert posts[1]["content"] == "SELECT 1;"


def test_a_fence_in_the_content_is_no_longer_special(posts):
    """Nothing is fenced now, so content containing ``` is just content."""
    discord_mcp.discord_send("here is ``` a fence")
    assert posts[0]["content"] == "here is ``` a fence"


def test_long_content_is_split_not_truncated(posts):
    """A truncated command looks complete and is not -- the failure this
    whole tool exists to avoid."""
    result = discord_mcp.discord_send("\n".join(f"line {n}" for n in range(600)))
    assert "sent in" in result
    rejoined = "\n".join(p["content"] for p in posts[1:])
    assert "line 0" in rejoined and "line 599" in rejoined


def test_no_part_carries_a_label_or_a_marker(posts):
    """The part count belongs in the note message. A '(part 2/3)' riding on
    a message full of shell is pasted into the shell."""
    discord_mcp.discord_send("\n".join(f"line {n}" for n in range(600)))
    assert "parts follow" in posts[0]["content"]
    assert all("part" not in p["content"] for p in posts[1:])
    assert all("**[" not in p["content"] for p in posts[1:])


def test_every_part_is_within_discords_limit(posts):
    discord_mcp.discord_send("x" * 9000)
    assert all(len(p["content"]) <= discord_mcp.LIMIT for p in posts)


def test_a_failed_part_reports_which_one(monkeypatch):
    monkeypatch.setattr(discord_mcp, "WEBHOOK", "https://discord.com/api/webhooks/1/secret")
    calls = []

    def flaky(request, timeout=None):
        calls.append(1)
        if len(calls) == 2:
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)
        return FakeResponse()

    monkeypatch.setattr(discord_mcp.urllib.request, "urlopen", flaky)
    result = discord_mcp.discord_send("\n".join(f"line {n}" for n in range(600)))
    assert "part 1 of" in result  # call 1 is the note, call 2 is the first part


def test_empty_content_is_refused(posts):
    assert discord_mcp.discord_send("   ") == "not sent: nothing to send"
    assert posts == []


# --- everyone mode, for a server of one -----------------------------------


def test_everyone_mode_pings_the_channel(sent, monkeypatch):
    monkeypatch.setattr(discord_mcp, "EVERYONE", True)
    monkeypatch.setattr(discord_mcp, "MENTION_ID", "")
    discord_mcp.discord_ask("which database?", source="isaac")
    assert sent["body"]["allowed_mentions"] == {"parse": ["everyone"], "users": []}
    assert sent["body"]["content"].startswith("@everyone ")


def test_a_status_update_cannot_ping_even_in_everyone_mode(sent, monkeypatch):
    """The whole point of the split: parsing is switched on only for the
    call that is deliberately pinging. Otherwise routine chatter wakes the
    channel and the pings stop meaning anything."""
    monkeypatch.setattr(discord_mcp, "EVERYONE", True)
    discord_mcp.discord_say("@everyone build finished", source="isaac")
    assert sent["body"]["allowed_mentions"] == {"parse": [], "users": []}


def test_a_username_is_not_a_mention(sent, monkeypatch):
    """Discord has no username form of a mention. Better no ping than a
    <@plarr> that posts as literal text and silently notifies nobody."""
    monkeypatch.setattr(discord_mcp, "EVERYONE", False)
    monkeypatch.setattr(discord_mcp, "MENTION_ID", "")
    discord_mcp.discord_ask("which one?", source="isaac")
    assert "<@" not in sent["body"]["content"]
    assert sent["body"]["allowed_mentions"]["users"] == []


def test_a_user_agent_is_always_sent(sent):
    """Not cosmetic. Discord's edge answers 403 to urllib's default one,
    before the request reaches the webhook -- measured, curl 204 against
    urllib 403 on an identical POST. A refactor that drops this header
    breaks every message with a status code that reads like a bad URL."""
    discord_mcp.discord_say("hello")
    assert sent["headers"]["User-agent"].startswith("discord-mcp")


# --- the boring bounds ----------------------------------------------------


def test_a_long_message_is_truncated_to_the_limit(sent):
    discord_mcp.discord_say("x" * 5000, source="isaac")
    body = sent["body"]["content"]
    assert len(body) <= discord_mcp.LIMIT
    assert body.endswith("[... truncated]")


def test_a_short_message_is_untouched(sent):
    discord_mcp.discord_say("done", source="isaac")
    assert sent["body"]["content"] == "**[isaac]** done"


def test_the_source_defaults_to_the_working_directory(sent, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    discord_mcp.discord_say("done")
    assert f"**[{tmp_path.name}]**" in sent["body"]["content"]


def test_a_rate_limit_is_retried_once(monkeypatch):
    monkeypatch.setattr(discord_mcp, "WEBHOOK", "https://discord.com/api/webhooks/1/secret")
    monkeypatch.setattr(discord_mcp.time, "sleep", lambda _: None)
    calls = []

    def flaky(request, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {"Retry-After": "1"}, None
            )
        return FakeResponse()

    monkeypatch.setattr(discord_mcp.urllib.request, "urlopen", flaky)
    assert discord_mcp.discord_say("hello").startswith("sent")
    assert len(calls) == 2
