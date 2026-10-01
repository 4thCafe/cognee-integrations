"""Behaviour needed for Cognee Cloud tenants: redirects and asynchronous builds."""

import re
import sys
from pathlib import Path

import httpx

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def test_every_http_client_follows_redirects():
    # The cloud gateway answers /api/v1/datasets with a 307 to the trailing-slash
    # URL; httpx does not follow redirects unless asked.
    offenders = []
    for path in list((PLUGIN_ROOT / "tools").glob("*.py")) + list(
        (PLUGIN_ROOT / "provider").glob("*.py")
    ):
        for match in re.finditer(r"httpx\.Client\(([^)]*)\)", path.read_text()):
            if "follow_redirects=True" not in match.group(1):
                offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == []


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _ItemsClient:
    """Scripted ``/processing-status`` answers (total, pending); 404 when unsupported."""

    def __init__(self, script, supported=True, status_script=None):
        self._script = list(script)
        self._supported = supported
        self._status_script = list(status_script or [])
        self.calls = []

    def _next(self, script):
        return script.pop(0) if len(script) > 1 else script[0]

    def get(self, url, **kwargs):
        self.calls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/processing-status"):
            if not self._supported:
                return httpx.Response(404, json={"detail": "not found"}, request=request)
            total, pending = self._next(self._script)
            return httpx.Response(
                200,
                json={
                    "total": total,
                    "completed": total - pending,
                    "pending": pending,
                    "items": [],
                },
                request=request,
            )
        if url.endswith("/datasets/status"):
            status = self._next(self._status_script)
            return httpx.Response(200, json={kwargs["params"]["dataset"]: status}, request=request)
        if url.endswith("/api/v1/datasets"):
            return httpx.Response(200, json=[{"id": "ds-1", "name": "notes"}], request=request)
        raise AssertionError(url)


def test_wait_ignores_the_stale_completed_status_and_waits_for_the_new_item():
    from tools.remember import wait_for_dataset

    # Dataset had 1 item before the call; the new one appears, then finishes.
    client = _ItemsClient([(1, 0), (2, 1), (2, 1), (2, 0)])
    clock = _Clock()
    status = wait_for_dataset(
        client, "http://x", {}, "ds-1", baseline_total=1, sleep=clock.sleep, now=clock.now
    )
    assert status == "completed"
    assert len(client.calls) == 4


def test_wait_accepts_deduplicated_content_after_the_grace_period():
    from tools.remember import DEDUP_GRACE, wait_for_dataset

    # Identical text is deduplicated: total never grows, nothing pending.
    client = _ItemsClient([(1, 0)])
    clock = _Clock()
    status = wait_for_dataset(
        client, "http://x", {}, "ds-1", baseline_total=1, sleep=clock.sleep, now=clock.now
    )
    assert status == "completed"
    assert clock.t >= DEDUP_GRACE


def test_wait_falls_back_to_dataset_status_when_per_item_route_is_missing():
    from tools.remember import wait_for_dataset

    client = _ItemsClient(
        [],
        supported=False,
        status_script=["DATASET_PROCESSING_STARTED", "DATASET_PROCESSING_COMPLETED"],
    )
    clock = _Clock()
    status = wait_for_dataset(
        client, "http://x", {}, "ds-1", baseline_total=0, sleep=clock.sleep, now=clock.now
    )
    assert status == "completed"
    assert any(u.endswith("/datasets/status") for u in client.calls)


def test_wait_fallback_does_not_trust_an_immediate_completed():
    from tools.remember import DEDUP_GRACE, wait_for_dataset

    client = _ItemsClient([], supported=False, status_script=["DATASET_PROCESSING_COMPLETED"])
    clock = _Clock()
    status = wait_for_dataset(
        client, "http://x", {}, "ds-1", baseline_total=None, sleep=clock.sleep, now=clock.now
    )
    assert status == "completed"
    assert clock.t >= DEDUP_GRACE  # only accepted after the grace period


def test_wait_reports_errored_and_gives_up_at_the_deadline():
    from tools.remember import wait_for_dataset

    client = _ItemsClient([], supported=False, status_script=["DATASET_PROCESSING_ERRORED"])
    assert (
        wait_for_dataset(client, "http://x", {}, "ds-1", baseline_total=None, sleep=lambda s: None)
        == "errored"
    )

    client = _ItemsClient([(2, 1)])
    clock = _Clock()
    status = wait_for_dataset(
        client,
        "http://x",
        {},
        "ds-1",
        baseline_total=1,
        sleep=clock.sleep,
        now=clock.now,
        deadline=5.0,
    )
    assert status == "running"


def test_resolve_dataset_id_and_item_count():
    from tools.remember import dataset_item_count, resolve_dataset_id

    client = _ItemsClient([(3, 0)])
    assert resolve_dataset_id(client, "http://x", {}, "notes") == "ds-1"
    assert resolve_dataset_id(client, "http://x", {}, "missing") == ""
    assert dataset_item_count(client, "http://x", {}, "ds-1") == 3
    assert dataset_item_count(_ItemsClient([], supported=False), "http://x", {}, "ds-1") is None
