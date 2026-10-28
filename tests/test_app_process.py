"""``python -m aicorp.app`` as a real program: one command starts everything, SIGTERM shuts everything down."""
import os
import signal
import subprocess
import sys
import time
import urllib.request

import psutil
import pytest

from aicorp import config, supervisor


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status, r.read().decode()
    except OSError:
        return None, ""


def test_one_command_starts_everything_and_sigterm_stops_it(tmp_path):
    home = config.ensure_layout(tmp_path / "data")
    env = {**os.environ, "AICORP_HOME": str(home), "PYTHONPATH": supervisor.package_root(), "AICORP_PORT": "18900"}
    env.pop("ANTHROPIC_API_KEY", None)
    app = subprocess.Popen([sys.executable, "-u", "-m", "aicorp.app", "--control-port", "18890"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        end = time.time() + 40
        status = None
        while time.time() < end:
            status, body = get("http://127.0.0.1:18890/api/state")
            if status == 200 and '"running"' in body:
                break
            time.sleep(0.3)
        assert status == 200
        end = time.time() + 40
        while time.time() < end and get("http://127.0.0.1:18900/")[0] != 200:
            time.sleep(0.3)
        assert get("http://127.0.0.1:18900/")[0] == 200
        assert "AICorp Control Panel" in get("http://127.0.0.1:18890/")[1]
        family = psutil.Process(app.pid).children(recursive=True)
        assert len(family) >= 3
        # nothing listens on anything but the loopback address
        for proc in [psutil.Process(app.pid), *family]:
            for conn in proc.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN:
                    assert conn.laddr.ip in ("127.0.0.1", "::1"), (proc.pid, conn.laddr)
    finally:
        app.send_signal(signal.SIGTERM)
        try:
            app.wait(40)
        except subprocess.TimeoutExpired:
            app.kill()
            pytest.fail("the app did not shut down on SIGTERM")
    time.sleep(0.5)
    left = [p.pid for p in family if psutil.pid_exists(p.pid) and psutil.Process(p.pid).status() != psutil.STATUS_ZOMBIE]
    assert not left
    assert get("http://127.0.0.1:18900/")[0] is None and get("http://127.0.0.1:18890/")[0] is None
    assert not (home / "runtime" / "supervisor.json").exists()
