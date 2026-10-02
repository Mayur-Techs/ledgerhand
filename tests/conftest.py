import pytest
import os
os.environ.setdefault("LH_TEST", "1")
os.environ.setdefault("LLM_MODE", "replay")
os.environ.setdefault("LB_ENFORCE_UNIQUE", "1")

import sys, subprocess, time, httpx
@pytest.fixture(scope="session")
def erp_server():
    env = os.environ.copy()
    env["LH_TEST"] = "1"
    env["LB_ENFORCE_UNIQUE"] = "1"
    env["LB_PASSWORD"] = "ledger123"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "mock_apps.ledgerbooks.app:app", "--port", "8003", "--no-access-log"],
        env=env
    )
    for _ in range(20):
        try:
            httpx.get("http://localhost:8003/login", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    yield proc
    proc.terminate()
    proc.wait()
