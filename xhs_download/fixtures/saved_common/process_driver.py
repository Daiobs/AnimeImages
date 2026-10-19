"""Isolated writer/reader used by tests_saved_common_process, never a server."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
NATIVE = Path(__file__).with_name("native_bootstrap.py")
IDENTITY = "synthetic-videofetch-0.9.1:" + hashlib.sha256(NATIVE.read_bytes()).hexdigest()
HOST = "www.acfun.cn"
SOURCE = "https://www.acfun.cn/v/ac100"
sys.path.insert(0, str(ROOT))


class OfflineViolation(BaseException):
    pass


def deny(*args, **kwargs):
    raise OfflineViolation("saved-common process forbids network/secret reads")


def audit(event, args):
    if event == "socket.bind":
        # urllib3 probes IPv6 during import; deny the bind without opening a port.
        raise OSError("offline fixture cannot bind")
    if event.startswith("socket.") and event != "socket.__new__":
        deny()
    if event == "subprocess.Popen":
        command = args[1]
        if not isinstance(command, (list, tuple)) or command[:3] != [sys.executable, "-B", str(NATIVE)]:
            deny()
    if event in {"os.system", "os.exec", "os.posix_spawn"}:
        deny()
    if event == "open" and isinstance(args[0], (str, bytes)):
        name = os.fsdecode(args[0])
        if Path(name).name == ".env":
            deny()


def fixed_dns(host, port, *args, **kwargs):
    assert host in {HOST, "media.example.org"}, host
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]


sys.addaudithook(audit)
socket.getaddrinfo = fixed_dns
socket.socket.connect = deny
original_exists = Path.exists
with patch.object(Path, "exists", lambda path: False if path.name == ".env" else original_exists(path)):
    import django
    django.setup()

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse
from xhs_download.models import (
    BatchParseRuntimeSetting, ProviderAccessMode, ProviderRuntimeSetting, UpstreamRoute, UpstreamRouteAudit,
)
from xhs_download.services.provider_lab import runtime
from xhs_download.services.saved_videodl import confirmation_scope
from xhs_download.services.upstream_routes import read_route, videodl_route_availability
from xhs_download.services.upstream_candidates import resolve_candidate_record
from xhs_download.services.quality_execution import execute_provider
from xhs_download.services.provider_lab.contracts import ProviderLabError

setup_test_environment()
events = []
operations = []
real_process = runtime._execute_external_process


def process(command, **kwargs):
    assert Path(command[1]).name == "videodl_worker.py", command
    assert kwargs["env"]["PYTHONNOUSERSITE"] == "1"
    assert Path(kwargs["cwd"]) != ROOT
    if "--check-only" in command:
        operation = "check"
    else:
        stream = kwargs["stdin"]
        position = stream.tell()
        request = json.load(stream)
        stream.seek(position)
        operation = request["operation"]
    operations.append(operation)
    result = real_process([command[0], "-B", str(NATIVE), *command[1:]], **kwargs)
    stream = kwargs["stderr"]
    position = stream.tell()
    stream.seek(0)
    for line in stream.read().decode().splitlines():
        if line.startswith("SAVED_COMMON_NATIVE "):
            event = json.loads(line.removeprefix("SAVED_COMMON_NATIVE "))
            assert event["identity"] == IDENTITY
            events.append(event)
    stream.seek(position)
    return result


def strict_client(user=None):
    client = Client(enforce_csrf_checks=True)
    if user is not None:
        client.force_login(user)
    response = client.get(reverse("home"))
    assert response.status_code == 200
    assert "csrftoken" in client.cookies
    return client


def post(client, payload, csrf=True):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value} if csrf else {}
    return client.post(reverse("upstream_routes"), json.dumps(payload),
                       content_type="application/json", **headers)


def writer(backend):
    call_command("migrate", verbosity=0, interactive=False)
    user = get_user_model().objects.create_user(username="saved-common-staff", is_staff=True)
    access = ProviderRuntimeSetting.load()
    access.third_party_mode = ProviderAccessMode.STAFF
    access.save()
    BatchParseRuntimeSetting.objects.update_or_create(singleton_key=1, defaults={
        "access_mode": ProviderAccessMode.PUBLIC, "ip_interval_seconds": 0,
        "global_minute_limit": 0, "daily_ip_limit": 0,
    })
    options = {"mode": "specific_backend", "backend_category": "common", "backend_id": backend}
    payload = {"source_url": SOURCE, "expected_revision": 0, "provider_order": ["videodl_repo"],
               "provider_options": {"videodl_repo": options},
               "sharing_confirmation": confirmation_scope(HOST, options)}
    anonymous = strict_client()
    assert post(anonymous, payload).status_code == 403
    staff = strict_client(user)
    assert post(staff, payload).json()["error_code"] == "CONFIGURATION_PERMISSION_REQUIRED"
    user.user_permissions.add(Permission.objects.get(codename="change_upstreamroute"))
    assert post(staff, payload, csrf=False).status_code == 403
    unconfirmed = {k: v for k, v in payload.items() if k != "sharing_confirmation"}
    rejected = post(staff, unconfirmed)
    assert rejected.status_code == 403
    assert rejected.json()["error_code"] == "COMMON_ROUTE_CONFIRMATION_REQUIRED"
    wrong_scope = {**payload, "sharing_confirmation": {
        **payload["sharing_confirmation"], "source_host": "other.example.org"}}
    assert post(staff, wrong_scope).json()["error_code"] == "COMMON_ROUTE_CONFIRMATION_REQUIRED"
    assert not UpstreamRoute.objects.exists()
    response = post(staff, payload)
    assert response.status_code == 200, response.content.decode()
    route = response.json()["route"]
    assert route["revision"] == 1 and route["active"]
    assert route["provider_options"]["videodl_repo"]["sharing_authorization"]["revision"] == 1
    assert UpstreamRouteAudit.objects.count() == 1
    assert "inventory" in operations and "backend" not in operations
    assert not [event for event in events if event["event"] == "parse"]
    return {"revision": 1, "auth_csrf_checked": True}


def reader(backend):
    cache.clear()
    route = read_route(SOURCE.replace("100", "200"))
    assert route["revision"] == 1 and route["active"]
    assert route["provider_options"]["videodl_repo"]["backend_id"] == backend
    assert UpstreamRouteAudit.objects.count() == 1
    availability = videodl_route_availability(SOURCE, route)
    assert availability["status"] == "available", availability
    assert len(availability["matches"]) == 1
    match = availability["matches"][0]
    assert match["extractor_id"] == backend and match["backend_category"] == "common"
    assert match["match_basis"] == "saved_exact_host_authorization"
    assert "manually selected Common" in match["label"]
    anonymous = strict_client()
    denied = anonymous.get(reverse("home_results"), {"q": SOURCE.replace("100", "201")})
    assert denied.status_code == 403, denied.status_code
    denied_batch = anonymous.post(reverse("batch_parse"), {"links": SOURCE.replace("100", "202")},
        HTTP_X_CSRFTOKEN=anonymous.cookies["csrftoken"].value)
    assert denied_batch.status_code == 200
    denied_rows = denied_batch.context["rows"]
    assert len(denied_rows) == 1 and denied_rows[0]["status"] != "success", denied_rows
    assert not denied_rows[0].get("upstream_candidates")
    assert "backend" not in operations
    assert not [event for event in events if event["event"] == "parse"]
    user = get_user_model().objects.get(username="saved-common-staff")
    staff = strict_client(user)
    home_source = SOURCE.replace("100", "300")
    home = staff.get(reverse("home_results"), {"q": home_source})
    assert home.status_code == 200, (home.status_code, home.context and home.context.get("error_message"))
    candidates = home.context["upstream_candidates"]
    assert candidates, home.context.get("error_message")
    assert all(item["provider_id"] == "videodl_repo" for item in candidates)
    assert home.context["upstream_route"]["revision"] == 1
    assert home.context["upstream_route"]["selected_provider"] == "videodl_repo"
    assert "data-upstream-candidates" in home.content.decode()
    for candidate in candidates:
        assert candidate["candidate_id"] in home.content.decode()
        record = resolve_candidate_record(candidate["resource_token"], candidate["candidate_id"])
        assert record["source_url"] == home_source
        assert record["provider_id"] == "videodl_repo"
    batch_source = SOURCE.replace("100", "400")
    batch = staff.post(reverse("batch_parse"), {"links": batch_source},
        HTTP_X_CSRFTOKEN=staff.cookies["csrftoken"].value)
    assert batch.status_code == 200
    rows = batch.context["rows"]
    assert len(rows) == 1 and rows[0]["status"] == "success", rows
    assert rows[0]["selected_provider"] == "videodl_repo"
    assert rows[0]["upstream_route"]["revision"] == 1
    assert rows[0]["upstream_candidates"]
    assert "data-upstream-candidates" in batch.content.decode()
    for candidate in rows[0]["upstream_candidates"]:
        assert candidate["candidate_id"] in batch.content.decode()
        record = resolve_candidate_record(candidate["resource_token"], candidate["candidate_id"])
        assert record["source_url"] == batch_source
    parsed = [event for event in events if event["event"] == "parse"]
    assert [(row["backend"], row["source"]) for row in parsed] == [
        (backend, home_source), (backend, batch_source)], parsed
    assert "match_dedicated" not in operations, operations
    assert UpstreamRouteAudit.objects.count() == 1
    assert read_route(SOURCE)["provider_options"] == route["provider_options"]
    reset = staff.delete(reverse("upstream_routes"), json.dumps({
        "source_host": HOST, "expected_revision": 1}), content_type="application/json",
        HTTP_X_CSRFTOKEN=staff.cookies["csrftoken"].value)
    assert reset.status_code == 200
    assert reset.json()["route"]["revision"] == 2
    before = len(operations)
    try:
        execute_provider("videodl_repo", SOURCE.replace("100", "500"),
                         backend=backend, saved_route_revision=1)
    except ProviderLabError as error:
        assert error.code == "PROVIDER_POLICY_REJECTED"
    else:
        raise AssertionError("revoked saved-route grant reached execution")
    assert len(operations) == before
    assert UpstreamRouteAudit.objects.count() == 2
    return {"revision": 1, "anonymous_denied": True, "home_candidates": len(candidates),
            "native_parses": len(parsed), "revoked_revision_rejected": True}


if __name__ == "__main__":
    phase, backend = sys.argv[1:]
    with patch.object(runtime, "_execute_external_process", side_effect=process):
        result = {"writer": writer, "reader": reader}[phase](backend)
    print("SAVED_COMMON_RESULT " + json.dumps({"phase": phase, "pid": os.getpid(),
        "fixture_identity": IDENTITY, "operations": operations, **result}))
