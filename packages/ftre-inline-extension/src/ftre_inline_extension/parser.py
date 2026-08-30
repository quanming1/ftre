"""Deterministic parser for the Markdown-like ftre inline syntax."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode

from .errors import ExtensionParseError
from .types import ExtensionRef, ExtensionSpan

_KIND = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
_TOKEN = re.compile(
    rf"!\[ftre:(?P<alt>{_KIND})\]"
    rf"\(ftre://(?P<version>v[0-9]+)/(?P<type>{_KIND})/(?P<name>{_KIND})"
    rf"(?:\?(?P<query>[^()\s]*))?\)"
)


class ExtensionParser:
    """Parse and serialize v1 references without invoking any handler."""

    max_token_bytes = 8 * 1024
    max_refs = 32

    def parse(self, text: str) -> tuple[ExtensionRef, ...]:
        if not isinstance(text, str) or not text:
            return ()
        refs: list[ExtensionRef] = []
        for match in _TOKEN.finditer(text):
            if len(refs) >= self.max_refs:
                break
            raw = match.group(0)
            if len(raw.encode("utf-8")) > self.max_token_bytes:
                continue
            if match.group("alt") != match.group("type"):
                continue
            try:
                refs.append(self._from_match(match))
            except (ExtensionParseError, ValueError):
                continue
        return tuple(refs)

    def parse_one(self, raw: str) -> ExtensionRef:
        match = _TOKEN.fullmatch(raw)
        if match is None:
            raise ExtensionParseError("invalid ftre extension token")
        if len(raw.encode("utf-8")) > self.max_token_bytes:
            raise ExtensionParseError("extension token is too large")
        if match.group("alt") != match.group("type"):
            raise ExtensionParseError("extension type mismatch")
        return self._from_match(match)

    def serialize(self, ref: ExtensionRef) -> str:
        if ref.version != "v1":
            raise ExtensionParseError(f"unsupported extension version: {ref.version}")
        query = urlencode(
            sorted((str(key), str(value)) for key, value in ref.args.items()),
            quote_via=quote,
        )
        suffix = f"?{query}" if query else ""
        return f"![ftre:{ref.type}](ftre://{ref.version}/{ref.type}/{ref.name}{suffix})"

    @staticmethod
    def _from_match(match: re.Match[str]) -> ExtensionRef:
        query = match.group("query") or ""
        args: dict[str, str] = {}
        for key, value in parse_qsl(query, keep_blank_values=True, strict_parsing=True):
            if not key:
                raise ExtensionParseError("extension query key is empty")
            args[key] = value
        return ExtensionRef(
            version=match.group("version"),
            type=match.group("type"),
            name=match.group("name"),
            args=args,
            raw=match.group(0),
            span=ExtensionSpan(match.start(), match.end()),
        )


__all__ = ["ExtensionParseError", "ExtensionParser"]
