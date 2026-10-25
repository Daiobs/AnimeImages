"""Local JSON/HTML fixtures only; no fetching or Provider result conversion."""

import argparse
import html
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import quote, unquote, urlsplit


REDACTED = "REDACTED"
_FIELDS = {"schema_version", "provider", "platform", "source_url", "provenance", "raw"}
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|(?<![:/])//)[^\s<>\"']+", re.I)
_URL_PARTS = re.compile(r"([a-z][a-z0-9+.-]*://|//)([^/?#]*)(.*)", re.I | re.S)
_PARAM_SEPARATOR = re.compile(r"(&(?:amp;|\#0*38;|\#x0*26;)?|;)", re.I)
_JSON_SCRIPT = re.compile(r"(<script\b[^>]*>)(\s*[\[{].*?)(</script\s*>)", re.I | re.S)
_ASSIGNMENT = re.compile(
    r"(?P<key>[A-Za-z_%][A-Za-z0-9_%.-]*)[\"']?\s*[:=]\s*"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&<>\"'{}]+)",
    re.S,
)
_AUTH = re.compile(r"\b(?:Bearer|Basic)\s+([^\s\"'<>;,]+)", re.I)
_COOKIE_LINE = re.compile(r"\b(?:set-cookie|cookie)\s*[:=]\s*([^\r\n<>]+)", re.I)
_KNOWN_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
    r"|\bsk-[A-Za-z0-9_-]{16,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
    r"|\b(?:xkeysib|xsmtpsib)-[A-Za-z0-9_-]{16,}\b",
    re.S,
)


class RecordingError(ValueError):
    """Only constant diagnostic codes, never input values or parser messages."""


def _decoded(value):
    for _ in range(8):
        decoded = html.unescape(unquote(value, errors="strict"))
        if decoded == value:
            return value
        value = decoded
    raise RecordingError("ENCODING_DEPTH")


def _name(value):
    return re.sub(r"[^a-z0-9]", "", _decoded(value).lower())


def _sensitive(name):
    name = _name(name)
    return any(part in name for part in (
        "cookie", "token", "secret", "password", "passwd", "credential",
        "authorization", "apikey", "accesskey", "privatekey", "signature",
    )) or name in {"sig", "sign", "auth", "authkey", "session", "sessionid",
                   "sessionkey", "sid", "jwt", "wrid", "xbogus", "abogus"}


def _replacement(value, checking):
    if checking and value != REDACTED:
        raise RecordingError("UNREDACTED_SECRET")
    return REDACTED


def _protected(value):
    """Credentials in a host/path must fail, never rewrite resource identity."""
    value = _decoded(value)
    try:
        _free_text(value, True)
    except RecordingError:
        raise RecordingError("CREDENTIAL_IN_HOST_OR_PATH") from None
    parts = value.split("/")
    if any(_sensitive(key) and item and item != REDACTED
           for key, item in zip(parts, parts[1:])):
        raise RecordingError("CREDENTIAL_IN_HOST_OR_PATH")


def _parameters(value, checking):
    # Keep original keys, duplicate parameters, separators and untouched values.
    parts = _PARAM_SEPARATOR.split(value)
    for index in range(0, len(parts), 2):
        key, equal, original = parts[index].partition("=")
        if not equal:
            parts[index] = _text(parts[index], checking)
            continue
        decoded = unquote(original.replace("+", " "), errors="strict")
        if _sensitive(key):
            clean = _replacement(decoded, checking)
        else:
            clean = _text(decoded, checking)
        if clean != decoded:
            parts[index] = key + "=" + quote(clean, safe="")
    return "".join(parts)


def _url(value, checking):
    match = _URL_PARTS.fullmatch(value)
    if not match:
        raise RecordingError("INVALID_URL")
    prefix, authority, rest = match.groups()
    parsed = urlsplit(value)
    if not parsed.hostname or any(c in value for c in "\r\n\t"):
        raise RecordingError("INVALID_URL")
    userinfo, at, host = authority.rpartition("@")
    if not at:
        host = authority
    path_query, fragment_mark, fragment = rest.partition("#")
    path, query_mark, query = path_query.partition("?")
    _protected(host)
    _protected(path)
    if at:
        userinfo = ":".join(_replacement(_decoded(part), checking)
                            for part in userinfo.split(":"))
        authority = userinfo + "@" + host
    query = _parameters(query, checking)
    fragment = _parameters(fragment, checking)
    return prefix + authority + path + query_mark + query + fragment_mark + fragment


def _free_text(value, checking):
    def field(match):
        original = match["value"]
        quoted = original[:1] in ("\"", "'")
        inner = original
        if quoted:
            if len(original) < 2 or original[-1] != original[0]:
                raise RecordingError("UNSUPPORTED_QUOTING")
            if original[0] == '"':
                try:
                    inner = json.loads(original)
                except ValueError:
                    raise RecordingError("UNSUPPORTED_QUOTING") from None
            else:
                if "\\" in original:
                    raise RecordingError("UNSUPPORTED_QUOTING")
                inner = original[1:-1]
        clean = _replacement(inner, checking) if _sensitive(match["key"]) else _text(inner, checking)
        if clean == inner:
            return match[0]
        if quoted:
            if original[0] == '"':
                clean = json.dumps(clean, ensure_ascii=False)
            else:
                if "'" in clean or "\\" in clean:
                    raise RecordingError("UNSUPPORTED_QUOTING")
                clean = "'" + clean + "'"
        return match[0][:match.start("value") - match.start()] + clean

    def header(match):
        clean = _replacement(match[1].strip(), checking)
        return match[0][:match.start(1) - match.start()] + clean

    def cookie(match):
        if any(char in match[1] for char in "\"'\\"):
            raise RecordingError("UNSUPPORTED_QUOTING")
        return header(match)

    value = _COOKIE_LINE.sub(cookie, value)
    value = _AUTH.sub(header, value)
    value = _ASSIGNMENT.sub(field, value)

    def known(match):
        return _replacement(match[0], checking)

    return _KNOWN_SECRET.sub(known, value)


def _text(value, checking, *, depth=0):
    if depth >= 8:
        raise RecordingError("ENCODING_DEPTH")
    stripped = value.strip()
    if stripped.startswith(("{", "[", '"')):
        try:
            parsed = _load_json(stripped)
        except json.JSONDecodeError:
            if html.unescape(value) == value and unquote(value, errors="strict") == value:
                raise RecordingError("UNSUPPORTED_QUOTING") from None
        else:
            _json_tree(parsed)
            cleaned = _walk(parsed, checking)
            if cleaned == parsed:
                return value
            start = len(value) - len(value.lstrip())
            return (value[:start] + json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
                    + value[start + len(stripped):])

    # Decode a standalone encoded fragment one layer, then restore that layer.
    # Literal URLs and markup keep their own path and attribute boundaries.
    if not _URL.search(value) and "<" not in value and ">" not in value:
        decoded = html.unescape(value)
        encode = lambda text: html.escape(text, quote=True)
        if decoded == value:
            decoded = unquote(value, errors="strict")
            encode = lambda text: quote(text, safe="")
        if decoded != value:
            clean = _text(decoded, checking, depth=depth + 1)
            if clean != decoded:
                if encode(decoded) != value:
                    raise RecordingError("UNSUPPORTED_ENCODING")
                return encode(clean)
            return value

    parts = []
    offset = 0
    for match in _JSON_SCRIPT.finditer(value):
        parts.extend((_text(value[offset:match.start()], checking), match[1],
                      _text(match[2], checking), match[3]))
        offset = match.end()
    if parts:
        parts.append(_text(value[offset:], checking))
        return "".join(parts)

    # Process URLs separately so header/text rules cannot erase safe query data.
    parts = []
    offset = 0
    for match in _URL.finditer(value):
        parts.append(_free_text(value[offset:match.start()], checking))
        original = match[0]
        candidate = original.rstrip(".,)]}")
        parts.append(_url(candidate, checking) + original[len(candidate):])
        offset = match.end()
    parts.append(_free_text(value[offset:], checking))
    return "".join(parts)


def _json_tree(value, seen=None):
    # Exact JSON types: never inspect attributes, properties or custom iterators.
    seen = set() if seen is None else seen
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise RecordingError("NON_FINITE_NUMBER")
        return
    if type(value) not in (dict, list):
        raise RecordingError("NOT_JSON")
    if id(value) in seen:
        raise RecordingError("JSON_CYCLE")
    seen.add(id(value))
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise RecordingError("NOT_JSON")
                _json_tree(key, seen)
                _json_tree(item, seen)
        else:
            for item in value:
                _json_tree(item, seen)
    finally:
        seen.remove(id(value))


def _schema(document):
    _json_tree(document)
    if type(document) is not dict or set(document) != _FIELDS:
        raise RecordingError("INVALID_ENVELOPE")
    provenance = document["provenance"]
    if (type(document["schema_version"]) is not int or document["schema_version"] != 1
            or any(type(document[key]) is not str or not _SLUG.fullmatch(document[key])
                   for key in ("provider", "platform"))
            or type(document["source_url"]) is not str
            or not re.match(r"https?://", document["source_url"], re.I)
            or re.search(r"\s", document["source_url"])
            or type(provenance) is not dict
            or set(provenance) != {"kind", "raw_kind", "complete"}
            or provenance["kind"] not in ("recorded", "synthetic")
            or provenance["raw_kind"] not in ("json", "html")
            or provenance["complete"] is not True
            or (provenance["raw_kind"] == "html" and type(document["raw"]) is not str)):
        raise RecordingError("INVALID_ENVELOPE")


def _walk(value, checking, mode="normal"):
    if type(value) is dict:
        names = {_name(key): key for key in value}
        named_value = "name" in names and "value" in names
        sensitive_pair = (named_value and type(value[names["name"]]) is str
                          and _sensitive(value[names["name"]]))
        result = {}
        for key, item in value.items():
            # Keys cannot be rewritten without changing the raw schema.
            _text(key, True)
            normalized = _name(key)
            child_mode = "normal" if mode == "headers" else mode
            if mode == "cookie" and named_value:
                child_mode = "secret" if normalized == "value" else "normal"
            elif mode == "cookie":
                child_mode = "secret"
            elif mode in ("normal", "headers"):
                if "cookie" in normalized:
                    child_mode = "cookie"
                elif _sensitive(key) or (sensitive_pair and normalized == "value"):
                    child_mode = "secret"
                elif normalized == "headers":
                    child_mode = "headers"
            result[key] = _walk(item, checking, child_mode)
        return result
    if type(value) is list:
        if mode == "headers":
            result = []
            for item in value:
                if type(item) is list:
                    if len(item) != 2 or type(item[0]) is not str:
                        raise RecordingError("UNSUPPORTED_HEADER_SHAPE")
                    _text(item[0], True)
                    child_mode = "secret" if _sensitive(item[0]) else "normal"
                    result.append([item[0], _walk(item[1], checking, child_mode)])
                else:
                    result.append(_walk(item, checking))
            return result
        return [_walk(item, checking, mode) for item in value]
    if mode in ("secret", "cookie"):
        # Still reject any embedded credential-bearing host/path before masking.
        if type(value) is str:
            for match in _URL.finditer(_decoded(value)):
                _url(match[0], False)
        return _replacement(value, checking)
    return _text(value, checking) if type(value) is str else value


def _safe_call(operation, *args):
    try:
        return operation(*args)
    except RecordingError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise RecordingError("INVALID_RECORDING") from None


def check(document):
    """Validate the envelope and independently scan fields, URLs and secret shapes."""
    def run():
        _schema(document)
        _url(document["source_url"], True)
        _walk(document, True)
    _safe_call(run)


def sanitize(document):
    """Return a new JSON tree. Refuse lossy exports and credentials in host/path."""
    def run():
        _schema(document)
        _url(document["source_url"], False)
        cleaned = _walk(document, False)
        check(cleaned)
        return cleaned
    return _safe_call(run)


def _load_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RecordingError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def constant(_):
        raise RecordingError("NON_FINITE_NUMBER")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def load_recording(path):
    """Strict UTF-8 JSON: duplicate keys and all non-finite numbers are errors."""
    def read():
        document = _load_json(Path(path).read_text(encoding="utf-8"))
        _schema(document)
        return document
    try:
        return _safe_call(read)
    except OSError:
        raise RecordingError("FILE_IO_ERROR") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise RecordingError("INVALID_ARGUMENTS")


def main(argv=None):
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    clean = commands.add_parser("sanitize")
    clean.add_argument("input")
    clean.add_argument("--output", required=True)
    validate = commands.add_parser("check")
    validate.add_argument("inputs", nargs="+")
    try:
        args = parser.parse_args(argv)
        if args.command == "sanitize":
            document = sanitize(load_recording(args.input))
            Path(args.output).write_text(json.dumps(document, ensure_ascii=True,
                                                   allow_nan=False, indent=2) + "\n",
                                         encoding="utf-8")
        else:
            for path in args.inputs:
                check(load_recording(path))
    except RecordingError as error:
        print("recording-error: " + str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("recording-error: FILE_IO_ERROR", file=sys.stderr)
        return 1
    print("OK: recording " + ("sanitized" if args.command == "sanitize" else "checked"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
