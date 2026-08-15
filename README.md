# discord-mcp

A Discord webhook exposed as an MCP server, so a Claude session can tell
you what it is doing and ping you when it is stuck.

Two tools:

| | |
|---|---|
| `discord_say(text, source)` | a status update, no ping |
| `discord_ask(question, source)` | a question, pings you |
| `discord_send(content, note, source)` | text alone in a message, to copy on a phone |

`discord_send` exists because terminal output cannot be selected on a
phone. A whole Discord message, however, copies with one long-press — so
the content is sent **alone in its own message**, and the `note` goes in a
separate message ahead of it. Anything sharing the message is pasted into
the shell along with the command.

## Why there is no code fence

The obvious design is a fenced block, and it is wrong here. Discord's
mobile client shows no copy button on a block, and copying a message
yields its **raw source** — so a fence puts ``` into what you paste.

Nothing is escaped either, for the same reason. Discord renders the
underscores in `DATABASE_URL` or `settle_day_of_month` as italics, and the
fix for that would be a backslash before each one — which is pasted along
with them. Verified both ways on the phone: a fenced send came back with
the fences and the note attached, an unfenced one came back byte for byte.

So the content arrives exactly as sent, and may look wrong in the channel
while doing so. That is the deliberate trade: the rendering is cosmetic,
the clipboard is not.

Long content is **split across messages, never truncated** — a command cut
short still looks complete, which is the failure the tool exists to avoid.
Each part is its own bare message, so each still copies in one press, and
the part count goes in the note message rather than on the parts. Five
parts is the cap; past that a phone notification is worse than scrollback.

Attachments were considered and rejected: they are immune to markdown, but
getting text out of one on mobile means opening it and selecting by hand,
which is the gesture this tool exists to avoid.

## Better than sending a script at all

When the target machine is one the session can already reach, write the
script to disk there and send a single short command that runs it. Nothing
markdown-significant crosses the wire, there is no split to reassemble,
and a generated secret stays out of a channel that keeps history forever.
Prefer this whenever it is available; `discord_send` is for when it is not.

`source` labels the message and defaults to the directory the session is
running in — with several sessions posting to one channel, an unlabelled
update is nearly useless.

## Setup

```bash
cp .env.example .env && chmod 600 .env   # paste your webhook URL
claude mcp add --scope user discord -- bash -c \
  'set -a; . /home/quentin/projects/discord-mcp/.env; set +a; \
   exec /home/quentin/isaac-code/.venv/bin/python /home/quentin/projects/discord-mcp/discord_mcp.py'
```

The secret lives in one 600-permission file rather than in
`~/.claude.json`, which is why the command sources `.env` instead of
passing `--env`.

## It cannot read replies

A webhook is send-only. There is no way to answer through this server, and
`discord_ask` says so in its own description so a model does not sit
waiting for something that will never arrive — it asks, then carries on
with whatever the answer does not block.

Making it two-way needs a bot application, a token, `GET
/channels/{id}/messages` polling or a gateway connection, and a channel
read scope. That is a different and larger piece of work; it is not a
flag on this one.

## Mentions are allowlisted, not parsed

The message body is written by a model, so `allowed_mentions` decides who
gets woken rather than the text doing it. `DISCORD_MENTION` takes:

- `everyone` — pings the channel. Fine on a server of one.
- a numeric user id — pings you specifically.
- anything else, including a username — no ping. Discord has no username
  form of a mention, so `<@plarr>` would post as literal text and notify
  nobody; empty is the honest result.

Even in `everyone` mode, **`discord_say` cannot ping**. Parsing is enabled
only on the call that is deliberately pinging, so an `@everyone` written
into ordinary generated prose posts as literal characters. That split is
the part worth not breaking — if routine chatter wakes the channel, the
pings stop meaning anything and get muted, which costs you the one feature
you wanted.

The webhook URL is likewise never included in a returned string —
`urllib`'s `HTTPError` carries the URL in its `str()`, so failures report
the status code only. Tool output goes into a model's context.

## Tests

```bash
/home/quentin/isaac-code/.venv/bin/python -m pytest test_discord_mcp.py -q
```

Twenty-three, no network.
