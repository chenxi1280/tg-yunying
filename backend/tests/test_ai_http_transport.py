import time
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import ai_http_transport as transport
from tests.ai_http_test_support import HTTP_IO_TEST_BUDGET_SECONDS, HTTP_SCHEDULING_TOLERANCE_SECONDS, local_http_server


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with local_http_server() as value:
        yield value


@pytest.fixture
def children(monkeypatch):
    observed = []
    original = transport.subprocess.Popen

    def start(*args, **kwargs):
        assert "qa-pipe-secret" not in str(args)
        process = original(*args, **kwargs)
        observed.append(process)
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", start)
    yield observed
    assert all(process.poll() is not None for process in observed)


def _read(url, *, seconds=0.5):
    request = urllib.request.Request(url, data=b"QA", headers={"Authorization": "Bearer qa-pipe-secret"})
    return transport.read_http(request, timeout=seconds, request_deadline=time.monotonic() + seconds)


def test_success_uses_pipe_and_reaps_worker(server, children):
    url, observed = server
    assert b'"ok": true' in _read(url + "/ok", seconds=2)
    assert observed == [("/ok", "Bearer qa-pipe-secret")]
    assert len(children) == 1 and children[0].returncode == 0


def test_existing_redirect_behavior_is_preserved_in_real_http(server, children):
    assert b'"ok": true' in _read(server[0] + "/redirect", seconds=2)
    assert [path for path, _authorization in server[1]] == ["/redirect", "/ok"]
    assert len(children) == 1 and children[0].returncode == 0


@pytest.mark.parametrize("path", ("/stall", "/drip", "/error-drip"))
def test_total_deadline_stops_silent_or_trickling_body_and_reaps_process(server, children, path):
    url, observed = server
    started = time.monotonic()
    with pytest.raises(transport.AiHttpResultUnknown) as caught:
        _read(url + path, seconds=HTTP_IO_TEST_BUDGET_SECONDS)
    elapsed = time.monotonic() - started
    assert elapsed < HTTP_IO_TEST_BUDGET_SECONDS + HTTP_SCHEDULING_TOLERANCE_SECONDS
    assert caught.value.local_termination_confirmed is True
    assert caught.value.local_worker_pid == children[0].pid
    assert children[0].returncode is not None
    assert observed[0][0] == path


def test_complete_http_error_body_and_retry_header_remain_available(server, children):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _read(server[0] + "/error", seconds=2)
    assert caught.value.code == 429
    assert caught.value.headers.get("retry-after") == "2"
    assert b"QA response" in caught.value.read()


def test_slow_request_does_not_hold_unrelated_fast_request(children, monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    slow_arrived = threading.Event()
    notify = lambda path: slow_arrived.set() if path == "/drip" else None
    with local_http_server(on_request=notify) as (url, _observed):
        with ThreadPoolExecutor(max_workers=2) as executor:
            slow = executor.submit(_read, url + "/drip", seconds=4)
            assert slow_arrived.wait(timeout=2), "slow request must enter real response I/O first"
            fast = executor.submit(_read, url + "/ok", seconds=3)
            assert b'"ok": true' in fast.result(timeout=3)
            assert not slow.done()
            with pytest.raises(transport.AiHttpResultUnknown):
                slow.result(timeout=4)
    assert len(children) == 2


def test_expired_budget_does_not_start_any_process(children):
    request = urllib.request.Request("http://127.0.0.1:1", data=b"QA")
    with pytest.raises(transport.AiHttpCallNotStarted):
        transport.read_http(request, timeout=1, request_deadline=time.monotonic() - 1)
    assert children == []


def test_explicit_connection_refusal_remains_a_pre_call_error(monkeypatch):
    import io
    import json
    from types import SimpleNamespace
    from app import ai_http_worker

    def refused(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError("QA refused before request"))

    spec = {"url": "http://127.0.0.1:1", "method": "POST", "headers": {}, "body": None, "timeout": 1}
    output = io.StringIO()
    monkeypatch.setattr(ai_http_worker.urllib.request, "build_opener", lambda handler: SimpleNamespace(open=refused))
    monkeypatch.setattr(ai_http_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(spec).encode())))
    monkeypatch.setattr(ai_http_worker.sys, "stdout", output)
    ai_http_worker.main()
    assert json.loads(output.getvalue()) == {"error": "connection_not_started", "error_type": "ConnectionRefusedError"}
    with pytest.raises(urllib.error.URLError) as caught:
        transport._decode_response(output.getvalue().encode(), request=None, process=SimpleNamespace(pid=0))
    assert isinstance(caught.value.reason, ConnectionRefusedError)


@pytest.mark.parametrize("reaped", (False, True))
def test_termination_status_is_explicit_not_assumed(reaped):
    from types import SimpleNamespace

    killed = []

    def communicate(*args, **kwargs):
        if killed and reaped:
            return b"", b""
        raise transport.subprocess.TimeoutExpired("QA child", 0.1)

    process = SimpleNamespace(pid=77, communicate=communicate,
        poll=lambda: -9 if killed and reaped else None, kill=lambda: killed.append(True))
    with pytest.raises(transport.AiHttpResultUnknown) as caught:
        transport._exchange(process, b"QA", deadline=time.monotonic() + 1, reserve=0.25)
    assert caught.value.local_termination_confirmed is reaped
    expected = "total_deadline" if reaped else "local_http_termination_unproven"
    assert expected in str(caught.value)


def test_broken_pipe_after_spawn_is_unknown_not_a_retryable_connection_error():
    from types import SimpleNamespace

    killed = []
    error = BrokenPipeError("QA pipe closed")

    def communicate(*args, **kwargs):
        if not killed:
            raise error
        return b"", b""

    process = SimpleNamespace(pid=78, communicate=communicate,
        poll=lambda: -9 if killed else None, kill=lambda: killed.append(True))
    with pytest.raises(transport.AiHttpResultUnknown) as caught:
        transport._exchange(process, b"QA", deadline=time.monotonic() + 1, reserve=0.25)
    assert caught.value.__cause__ is error
    assert caught.value.local_termination_confirmed


def test_http_error_status_below_400_is_preserved():
    import base64
    import json
    from types import SimpleNamespace

    envelope = {"status": 302, "http_error": True, "body": base64.b64encode(b"redirect loop").decode(), "headers": []}
    with pytest.raises(urllib.error.HTTPError) as caught:
        transport._decode_response(json.dumps(envelope).encode(), request=SimpleNamespace(full_url="http://localhost"),
                                   process=SimpleNamespace(pid=0))
    assert caught.value.code == 302


@pytest.mark.parametrize("redirected", (False, True))
def test_connection_refusal_after_redirect_is_not_a_pre_call_error(monkeypatch, redirected):
    from types import SimpleNamespace
    from app import ai_http_worker

    def build_opener(handler):
        def open_request(request, **kwargs):
            if redirected:
                handler.redirect_request(request, None, 302, "Found", {}, "http://localhost/second")
            raise urllib.error.URLError(ConnectionRefusedError("QA refusal"))
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(ai_http_worker.urllib.request, "build_opener", build_opener)
    spec = {"url": "http://localhost/first", "method": "POST", "headers": {}, "body": None, "timeout": 1}
    expected = ai_http_worker.RedirectResultUnknown if redirected else urllib.error.URLError
    with pytest.raises(expected):
        ai_http_worker.exchange(spec)


@pytest.mark.parametrize("reaped", (False, True))
def test_reap_io_error_preserves_unknown_and_actual_termination(reaped):
    from types import SimpleNamespace

    def broken(**kwargs):
        raise BrokenPipeError("QA reap failure")

    process = SimpleNamespace(pid=79, poll=lambda: -9 if reaped else None, kill=lambda: None, communicate=broken)
    with pytest.raises(transport.AiHttpResultUnknown) as caught:
        transport._terminate(process, deadline=time.monotonic() + 1)
    assert caught.value.local_termination_confirmed is reaped
    assert isinstance(caught.value.__cause__, BrokenPipeError)


def test_close_failure_closes_other_pipes_and_preserves_unknown():
    from types import SimpleNamespace

    closed = []

    def broken():
        raise OSError("QA pipe close failure")

    process = SimpleNamespace(pid=80, poll=lambda: 0, stdin=SimpleNamespace(close=broken),
        stdout=SimpleNamespace(close=lambda: closed.append("stdout")),
        stderr=SimpleNamespace(close=lambda: closed.append("stderr")))
    with pytest.raises(transport.AiHttpResultUnknown) as caught:
        transport._close_pipes(process)
    assert closed == ["stdout", "stderr"]
    assert caught.value.local_termination_confirmed
    assert isinstance(caught.value.__cause__, OSError)
