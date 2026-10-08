"""@mention parsing (FROZEN §12.8: @mentions are V1). Pure: text in, candidate handles out; resolution
against Users is the command's job. Accepted forms (BUILD-08.plan.md Risk B2):

    @"Ada Lovelace"      a quoted display name (names contain spaces)
    @ada.lovelace        a single ASCII token, matched as a display name without spaces
    @ada@example.com     an email address

Handles are returned in order of first appearance, de-duplicated case-insensitively, at most
`MAX_MENTIONS`; a comment body is capped at `MAX_COMMENT_CHARS` (request amplification guard)."""

from __future__ import annotations

import re

MAX_COMMENT_CHARS = 10_000
MAX_MENTIONS = 50

_MENTION = re.compile(r'(?<![\w@])@(?:"([^"\n]{1,120})"|([A-Za-z0-9][A-Za-z0-9._+-]*(?:@[A-Za-z0-9.-]+)?))')


def parse_mentions(body: str) -> list[str]:
    seen: set[str] = set()
    handles: list[str] = []
    for quoted, bare in _MENTION.findall(body):
        handle = (quoted or bare).strip().rstrip(".,;:!?")
        if not handle:
            continue
        key = handle.lower()
        if key not in seen:
            seen.add(key)
            handles.append(handle)
            if len(handles) == MAX_MENTIONS:
                break
    return handles
