#!/usr/bin/env python3
"""Read-only validation for the canonical AnimeImages Nginx configuration."""

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple


PASS = 0
DRIFT = 1
CANNOT_VERIFY = 2
PROJECT_ROOT = Path(__file__).resolve().parents[3]
NGINX_ROOT = PROJECT_ROOT / "ops" / "nginx"
MANIFEST_PATH = NGINX_ROOT / "manifest.json"
SITE_TEMPLATE_PATH = NGINX_ROOT / "sites" / "download.cosflow.icu.conf.template"
MEDIA_SNIPPET_PATH = NGINX_ROOT / "snippets" / "animeimages-v1-media.conf"
SITE_PLACEHOLDER = "{{ANIMEIMAGES_SERVER_IP}}"
PRODUCTION_MEDIA_INCLUDE = "/etc/nginx/snippets/animeimages-v1-media.conf"
LOCAL_APP_UPSTREAM = "http://127.0.0.1:8000"
TELEMETRY_PATH = "/media/browser-download-telemetry/"
SITE_LOCATION_ARGS = {
    ("/static/",),
    ("/media/proxy-image/",),
    ("/media/download-images/",),
    ("/media/download-tasks/",),
    ("/media/",),
    ("/",),
}
SNIPPET_LOCATION_ARGS = {
    ("=", TELEMETRY_PATH),
    ("^~", "/media/douyin-continuation/"),
    ("^~", "/media/download-prepare/"),
    ("^~", "/media/download-status/"),
    ("^~", "/media/download-stream/"),
}
REQUIRED_PROXY_HEADERS = {
    ("Host", "$host"),
    ("X-Real-IP", "$remote_addr"),
    ("X-Forwarded-For", "$proxy_add_x_forwarded_for"),
    ("X-Forwarded-Proto", "$scheme"),
    ("X-Request-ID", "$request_id"),
}


class ConfigInputError(ValueError):
    """The supplied input cannot be safely interpreted."""


class ConfigDriftError(ValueError):
    """A managed file or semantic invariant differs from the baseline."""


@dataclass(frozen=True)
class Directive:
    name: str
    args: Tuple[str, ...]
    children: Optional[Tuple["Directive", ...]] = None


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ConfigInputError(f"cannot read {path}: {error.strerror or error}") from error


def render_site_template(template: str, server_ip: str) -> str:
    try:
        normalized_ip = str(ipaddress.ip_address(server_ip))
    except ValueError as error:
        raise ConfigInputError("ANIMEIMAGES_SERVER_IP must be a valid IP address") from error
    occurrences = template.count(SITE_PLACEHOLDER)
    if occurrences != 2:
        raise ConfigInputError(
            f"site template must contain exactly two {SITE_PLACEHOLDER} placeholders"
        )
    return template.replace(SITE_PLACEHOLDER, normalized_ip)


def tokenize_nginx(text: str) -> Tuple[str, ...]:
    tokens = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if char in "{};":
            tokens.append(char)
            index += 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            value = []
            while index < length:
                char = text[index]
                if char == "\\" and index + 1 < length:
                    value.extend((char, text[index + 1]))
                    index += 2
                    continue
                if char == quote:
                    index += 1
                    break
                value.append(char)
                index += 1
            else:
                raise ConfigInputError("unterminated quoted value in Nginx configuration")
            tokens.append("".join(value))
            continue

        value = []
        while index < length:
            char = text[index]
            if char.isspace() or char in "{};#":
                break
            value.append(char)
            index += 1
        if not value:
            raise ConfigInputError("unable to tokenize Nginx configuration")
        tokens.append("".join(value))
    return tuple(tokens)


def parse_nginx(text: str) -> Tuple[Directive, ...]:
    tokens = tokenize_nginx(text)
    position = 0

    def parse_scope(expect_closing_brace: bool) -> Tuple[Directive, ...]:
        nonlocal position
        directives = []
        header = []
        while position < len(tokens):
            token = tokens[position]
            position += 1
            if token == "}":
                if header:
                    raise ConfigInputError("directive is missing ';' before '}'")
                if not expect_closing_brace:
                    raise ConfigInputError("unexpected closing brace")
                return tuple(directives)
            if token == ";":
                if not header:
                    raise ConfigInputError("empty Nginx directive")
                directives.append(Directive(header[0], tuple(header[1:])))
                header = []
                continue
            if token == "{":
                if not header:
                    raise ConfigInputError("block is missing a directive name")
                children = parse_scope(True)
                directives.append(Directive(header[0], tuple(header[1:]), children))
                header = []
                continue
            header.append(token)

        if header:
            raise ConfigInputError("directive is missing a terminating ';'")
        if expect_closing_brace:
            raise ConfigInputError("Nginx block is missing a closing brace")
        return tuple(directives)

    parsed = parse_scope(False)
    if position != len(tokens):
        raise ConfigInputError("unexpected trailing Nginx tokens")
    return parsed


def walk(directives: Iterable[Directive]) -> Iterable[Directive]:
    for directive in directives:
        yield directive
        if directive.children is not None:
            yield from walk(directive.children)


def direct_children(directive: Directive, name: str) -> Tuple[Directive, ...]:
    return tuple(child for child in directive.children or () if child.name == name)


def _locations(directives: Iterable[Directive], args: Sequence[str]) -> Tuple[Directive, ...]:
    expected = tuple(args)
    return tuple(
        directive
        for directive in walk(directives)
        if directive.name == "location" and directive.args == expected
    )


def _single(values: Sequence[Directive], label: str) -> Directive:
    if len(values) != 1:
        raise ConfigDriftError(f"expected exactly one {label}; found {len(values)}")
    return values[0]


def _has_directive(directive: Directive, name: str, args: Sequence[str]) -> bool:
    expected = tuple(args)
    return any(
        child.name == name and child.args == expected
        for child in directive.children or ()
    )


def _https_server(site: Tuple[Directive, ...]) -> Directive:
    candidates = []
    for server in (directive for directive in site if directive.name == "server"):
        server_names = direct_children(server, "server_name")
        listens = direct_children(server, "listen")
        if (
            any("download.cosflow.icu" in item.args for item in server_names)
            and any("443" in item.args for item in listens)
        ):
            candidates.append(server)
    return _single(candidates, "download.cosflow.icu HTTPS server block")


def _validate_location_set(
    directives: Iterable[Directive], expected: set, label: str
) -> None:
    locations = tuple(item for item in directives if item.name == "location")
    actual = {item.args for item in locations}
    if actual != expected or len(locations) != len(expected):
        raise ConfigDriftError(f"{label} location set differs from the baseline")


def validate_baseline(site_text: str, snippet_text: str) -> None:
    site = parse_nginx(site_text)
    snippet = parse_nginx(snippet_text)
    server = _https_server(site)

    _validate_location_set(server.children or (), SITE_LOCATION_ARGS, "site")
    _validate_location_set(snippet, SNIPPET_LOCATION_ARGS, "media snippet")

    telemetry = _single(
        _locations(snippet, ("=", TELEMETRY_PATH)),
        "exact browser telemetry location",
    )
    if not _has_directive(telemetry, "proxy_pass", (LOCAL_APP_UPSTREAM,)):
        raise ConfigDriftError("telemetry location does not proxy to the Django upstream")
    configured_headers = {
        item.args
        for item in direct_children(telemetry, "proxy_set_header")
    }
    if configured_headers != REQUIRED_PROXY_HEADERS:
        raise ConfigDriftError("telemetry proxy headers differ from the baseline")

    broad_media = _single(
        tuple(
            child
            for child in server.children or ()
            if child.name == "location" and child.args == ("/media/",)
        ),
        "broad /media/ protection location",
    )
    if not _has_directive(broad_media, "return", ("404",)):
        raise ConfigDriftError("broad /media/ location must return 404")
    if direct_children(broad_media, "proxy_pass"):
        raise ConfigDriftError("broad /media/ location must not proxy requests")

    server_children = tuple(server.children or ())
    include_indexes = [
        index
        for index, item in enumerate(server_children)
        if item.name == "include" and item.args == (PRODUCTION_MEDIA_INCLUDE,)
    ]
    broad_indexes = [
        index
        for index, item in enumerate(server_children)
        if item.name == "location" and item.args == ("/media/",)
    ]
    if len(include_indexes) != 1:
        raise ConfigDriftError("AnimeImages media snippet include must appear exactly once")
    if include_indexes[0] >= broad_indexes[0]:
        raise ConfigDriftError("media snippet include must precede the broad /media/ deny")

    static_location = _single(
        tuple(
            child
            for child in server_children
            if child.name == "location" and child.args == ("/static/",)
        ),
        "static location",
    )
    if not _has_directive(static_location, "alias", ("/srv/animeimages/staticfiles/",)):
        raise ConfigDriftError("static location does not use the production staticfiles path")

    root_location = _single(
        tuple(
            child
            for child in server_children
            if child.name == "location" and child.args == ("/",)
        ),
        "application root location",
    )
    if not _has_directive(root_location, "proxy_pass", (LOCAL_APP_UPSTREAM,)):
        raise ConfigDriftError("application root does not proxy to Django")

    all_proxy_passes = tuple(
        directive
        for directive in walk(site + snippet)
        if directive.name == "proxy_pass"
    )
    if any(directive.args != (LOCAL_APP_UPSTREAM,) for directive in all_proxy_passes):
        raise ConfigDriftError(
            "every proxy_pass must contain only the local Django upstream"
        )


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    try:
        manifest = json.loads(_read_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigInputError(f"invalid manifest: {error}") from error
    if manifest.get("schema_version") != 1:
        raise ConfigInputError("manifest schema_version must be 1")
    entries = manifest.get("managed_files")
    if not isinstance(entries, list) or len(entries) != 2:
        raise ConfigInputError("manifest must contain the two managed AnimeImages files")
    required_fields = {
        "logical_name",
        "repo_path",
        "production_path",
        "ownership",
        "mode",
        "comparison_mode",
        "variables",
        "required",
    }
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not required_fields.issubset(entry):
            raise ConfigInputError("manifest entry is missing required fields")
        names.add(entry["logical_name"])
        repo_path = PROJECT_ROOT / entry["repo_path"]
        if not repo_path.is_file():
            raise ConfigInputError(f"managed repository file is missing: {repo_path}")
        if entry["ownership"] != "root:root" or entry["mode"] != "0644":
            raise ConfigInputError("managed Nginx files must declare root:root 0644")
        if entry["comparison_mode"] not in {"exact", "rendered_exact"}:
            raise ConfigInputError("unsupported manifest comparison mode")
        if entry["required"] is not True:
            raise ConfigInputError("managed Nginx files must be required")
    expected_names = {"animeimages_site_vhost", "animeimages_media_snippet"}
    if names != expected_names:
        raise ConfigInputError("manifest logical names do not match the baseline")
    return manifest


def validate_repository() -> Tuple[bytes, bytes]:
    load_manifest()
    template_bytes = _read_bytes(SITE_TEMPLATE_PATH)
    snippet_bytes = _read_bytes(MEDIA_SNIPPET_PATH)
    try:
        template_text = template_bytes.decode("utf-8")
        snippet_text = snippet_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigInputError("canonical Nginx files must be UTF-8") from error
    rendered = render_site_template(template_text, "192.0.2.10")
    validate_baseline(rendered, snippet_text)
    return template_bytes, snippet_bytes


def compare_exact(canonical: bytes, live_path: Path, label: str) -> None:
    live = _read_bytes(live_path)
    canonical_hash = sha256_bytes(canonical)
    live_hash = sha256_bytes(live)
    print(f"{label}: repo_sha256={canonical_hash} live_sha256={live_hash}")
    if canonical != live:
        raise ConfigDriftError(f"{label} differs from the canonical repository file")


def split_effective_dump(text: str) -> dict:
    marker = re.compile(r"^# configuration file (.+):$")
    sections = {}
    current = None
    lines = []
    for line in text.splitlines(keepends=True):
        match = marker.match(line.rstrip("\n"))
        if match:
            if current is not None:
                sections[current] = "".join(lines)
            current = match.group(1)
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "".join(lines)
    if not sections:
        raise ConfigInputError("effective dump has no '# configuration file' sections")
    return sections


def validate_effective_dump(path: Path) -> None:
    try:
        text = _read_bytes(path).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigInputError("effective Nginx dump must be UTF-8") from error
    sections = split_effective_dump(text)
    site_candidates = [
        content
        for name, content in sections.items()
        if name.endswith("/sites-enabled/animeimages")
        or name.endswith("/sites-available/animeimages")
    ]
    snippet_candidates = [
        content
        for name, content in sections.items()
        if name.endswith("/snippets/animeimages-v1-media.conf")
    ]
    site_text = _single_text(site_candidates, "AnimeImages site section")
    snippet_text = _single_text(snippet_candidates, "AnimeImages media snippet section")
    validate_baseline(site_text, snippet_text)


def _single_text(values: Sequence[str], label: str) -> str:
    if len(values) != 1:
        raise ConfigInputError(f"effective dump must contain exactly one {label}")
    return values[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only AnimeImages Nginx baseline and drift validation."
    )
    parser.add_argument("--live-snippet", type=Path)
    parser.add_argument("--live-site", type=Path)
    parser.add_argument("--server-ip")
    parser.add_argument("--effective-config", type=Path)
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        template_bytes, snippet_bytes = validate_repository()
        print(
            "repository: PASS "
            f"site_template_sha256={sha256_bytes(template_bytes)} "
            f"media_snippet_sha256={sha256_bytes(snippet_bytes)}"
        )
        if args.live_snippet:
            compare_exact(snippet_bytes, args.live_snippet, "animeimages_media_snippet")
        if args.live_site:
            if not args.server_ip:
                raise ConfigInputError("--server-ip is required with --live-site")
            template_text = template_bytes.decode("utf-8")
            rendered = render_site_template(template_text, args.server_ip).encode("utf-8")
            compare_exact(rendered, args.live_site, "animeimages_site_vhost")
        elif args.server_ip:
            raise ConfigInputError("--server-ip is only valid with --live-site")
        if args.effective_config:
            validate_effective_dump(args.effective_config)
            print("effective_config: PASS")
    except ConfigDriftError as error:
        print(f"DRIFT: {error}", file=sys.stderr)
        return DRIFT
    except ConfigInputError as error:
        print(f"CANNOT_VERIFY: {error}", file=sys.stderr)
        return CANNOT_VERIFY
    print("NGINX_CONFIG_DRIFT=0")
    return PASS


if __name__ == "__main__":
    sys.exit(run())
