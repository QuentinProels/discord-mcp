"""A Discord webhook, exposed to MCP.

For long-running agent work: a session posts what it is doing, and pings
you when it needs an answer. Send-only by design -- a webhook cannot read
a channel, so there is no way to reply through this. Answering from
Discord needs a bot token and the gateway; see README.md.

Register it with:

    claude mcp add --scope user discord -- bash -c \
      'set -a; . /home/quentin/projects/discord-mcp/.env; set +a; \
       exec /path/to/python /home/quentin/projects/discord-mcp/discord_mcp.py'

Reads DISCORD_WEBHOOK_URL (required), DISCORD_MENTION (your numeric user
id, so discord_ask can ping you) and DISCORD_MCP_USERNAME (the display
name posts appear under). No secret is ever written to disk by this
process, and the webhook URL is never included in a returned message --
it is a credential, and tool output goes into a model's context.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# DISCORD_MENTION takes a numeric user id, the <@id> form (both are things
# you paste), or the literal "everyone". A username is not a mention on
# Discord and never resolves to one, so it lands here as empty -- which
# means no ping rather than a broken one.
_MENTION = os.environ.get("DISCORD_MENTION", "").strip().lstrip("@").lower()
EVERYONE = _MENTION == "everyone"
MENTION_ID = "" if EVERYONE else re.sub(r"\D", "", _MENTION)
USERNAME = os.environ.get("DISCORD_MCP_USERNAME", "").strip()

LIMIT = 2000  # Discord rejects a longer content field outright.
MAX_PARTS = 5  # past this a phone notification is worse than a scrollback
TIMEOUT = 15

# Discord's edge returns 403 to urllib's default "Python-urllib/3.x" before
# the request ever reaches the webhook. Measured: identical POST via curl
# 204, via urllib 403, difference was this header alone.
USER_AGENT = "discord-mcp (https://github.com/QuentinProels, 1.0)"

mcp = FastMCP("discord")


def _label(source: str) -> str:
    """Which session is talking.

    Defaults to the directory the server was spawned in, which is the
    client's project directory -- with several sessions posting to one
    channel, an unlabelled status update is nearly useless.
    """
    name = source.strip() or Path.cwd().name
    return f"**[{name}]** " if name else ""


def _chunks(text: str, size: int) -> list[str]:
    """Split for sending, on line boundaries where one is near enough.

    Splitting rather than truncating, because this carries things meant to
    be copied and run. A command cut short is worse than one sent in two
    pieces -- it looks complete and is not.
    """
    parts: list[str] = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        cut = window.rfind("\n")
        if cut < size // 2:  # no line break worth honouring; hard cut
            cut = size
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


def _fit(text: str) -> str:
    if len(text) <= LIMIT:
        return text
    marker = "\n[... truncated]"
    return text[: LIMIT - len(marker)] + marker


def _retry_after(error: urllib.error.HTTPError) -> float:
    """Discord says how long to wait; believe it, within reason."""
    try:
        body = json.loads(error.read().decode() or "{}")
        wait = float(body.get("retry_after", 0))
    except (ValueError, OSError):
        wait = 0.0
    if wait <= 0:
        try:
            wait = float(error.headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            wait = 1.0
    return min(max(wait, 0.5), 10.0)


def _send(content: str, ping: bool) -> str:
    if not WEBHOOK:
        return ("not sent: DISCORD_WEBHOOK_URL is not set in this server's "
                "environment.")

    # The body is model-written, so who gets woken is decided here rather
    # than parsed out of the text. Even in everyone mode a status update
    # cannot ping: parsing is enabled only on the call that is deliberately
    # pinging, so an "@everyone" written into ordinary generated prose
    # posts as literal characters.
    if ping and EVERYONE:
        mentions = {"parse": ["everyone"], "users": []}
    elif ping and MENTION_ID:
        mentions = {"parse": [], "users": [MENTION_ID]}
    else:
        mentions = {"parse": [], "users": []}

    payload = {"content": _fit(content), "allowed_mentions": mentions}
    if USERNAME:
        payload["username"] = USERNAME

    data = json.dumps(payload).encode()
    for attempt in (1, 2):
        request = urllib.request.Request(
            WEBHOOK, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                return f"sent ({response.status})"
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt == 1:
                time.sleep(_retry_after(error))
                continue
            # Only the status code. HTTPError's str() carries the URL, and
            # the URL is the credential.
            return f"not sent: discord returned {error.code}"
        except urllib.error.URLError as error:
            return f"not sent: {error.reason}"
    return "not sent: rate limited twice"


@mcp.tool()
def discord_say(text: str, source: str = "") -> str:
    """Post a status update to Discord. Does not ping.

    For progress a human may want but does not have to act on: what you
    started, what finished, what you found. Use it when work will run
    long enough that silence is worse than an interruption.

    Args:
        text: what to say, in plain sentences. Over 2000 characters is
            truncated, so lead with the point.
        source: which project this is about. Defaults to the directory
            the session is running in.
    """
    return _send(_label(source) + text.strip(), ping=False)


@mcp.tool()
def discord_send(content: str, note: str = "", source: str = "") -> str:
    """Send text to Discord as its own message, to be copied on a phone.

    For getting something off the terminal and onto a phone: a command to
    run elsewhere, a connection string, a log extract, a config block.
    Terminal output cannot be selected on a phone, but a whole Discord
    message can be copied with one long-press -- so the content is sent
    alone, with nothing else in the message.

    That is why there is no code fence. Copying a message on mobile yields
    its raw source, so a fence would put ``` into what you paste. Nothing
    is escaped either, for the same reason: a backslash added to protect
    an underscore would be pasted along with it. The content arrives byte
    for byte. It may *look* wrong in the channel -- Discord will render
    underscores in a shell command as italics -- but what you copy is
    exact, and that is the half that matters.

    Long content is split across messages rather than truncated, because a
    command cut short still looks complete. Each part is its own bare
    message, so each one still copies in a single press.

    Args:
        content: the text itself, sent verbatim and alone.
        note: one line of explanation. Posted as a separate message, never
            in with the content -- anything sharing the message ends up in
            what gets pasted into a shell.
        source: which project this is about. Defaults to the directory
            the session is running in.
    """
    body = content.rstrip()
    if not body:
        return "not sent: nothing to send"

    parts = _chunks(body, LIMIT)
    total = min(len(parts), MAX_PARTS)
    parts = parts[:MAX_PARTS]

    # The note and the part count go in one message ahead of the content,
    # never on a message carrying content. A long-press copies the whole
    # message, so a header sentence riding along with a command is pasted
    # into the shell with it.
    lead = note.strip()
    if total > 1:
        counter = f"{total} parts follow, in order."
        lead = f"{lead} ({counter})" if lead else counter
    if lead:
        result = _send(_label(source) + lead, ping=False)
        if not result.startswith("sent"):
            return f"{result} (the note; no content was sent)"

    for index, part in enumerate(parts, start=1):
        result = _send(part, ping=False)
        if not result.startswith("sent"):
            return f"{result} (part {index} of {total})"

    detail = f"sent in {total} messages" if total > 1 else "sent"
    if len(_chunks(body, LIMIT)) > MAX_PARTS:
        detail += f"; stopped after {MAX_PARTS} parts"
    return detail


@mcp.tool()
def discord_ask(question: str, source: str = "") -> str:
    """Post a question to Discord and ping, because you are blocked.

    Only for a genuine decision you cannot make yourself -- the ping is
    what makes it useful, and a ping spent on something you could have
    decided is a ping that gets muted. There is no way to read the reply
    through this server: ask, then continue on everything the answer does
    not block, and pick the answer up wherever you normally would.

    Args:
        question: what you need decided, and the options if there are any.
        source: which project this is about. Defaults to the directory
            the session is running in.
    """
    if EVERYONE:
        mention = "@everyone "
    elif MENTION_ID:
        mention = f"<@{MENTION_ID}> "
    else:
        mention = ""
    return _send(f"{mention}{_label(source)}{question.strip()}", ping=True)


if __name__ == "__main__":
    mcp.run()
