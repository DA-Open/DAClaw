"""Check the POSIX watchdog without arming a real alarm in the test process."""

from contextlib import contextmanager
import sys
from unittest.mock import Mock, call

import pytest

from tests import conftest


@pytest.mark.skipif(sys.platform == "win32", reason="the SIGALRM watchdog runs on POSIX")
@pytest.mark.parametrize("seconds", [30, pytest.param(90, marks=pytest.mark.integration)])
def test_timeout_uses_marker_and_restores_handler(request, monkeypatch, seconds):
    signals = Mock()
    monkeypatch.setattr(conftest, "signal", signals)
    watchdog = contextmanager(conftest._enforce_test_timeout.__wrapped__)
    with pytest.raises(TimeoutError, match=f"^Test exceeded {seconds} second timeout$"):
        with watchdog(request):
            signals.alarm.assert_called_once_with(seconds)
            handler = signals.signal.call_args.args[1]
            handler(signals.SIGALRM, None)
    assert signals.alarm.call_args_list == [call(seconds), call(0)]
    assert signals.signal.call_args_list == [
        call(signals.SIGALRM, handler), call(signals.SIGALRM, signals.signal.return_value),
    ]
