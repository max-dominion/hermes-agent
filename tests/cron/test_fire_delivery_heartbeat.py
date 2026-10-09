"""Real-store lease renewal must coexist with fenced delivery, never revoke it."""
import threading

import pytest


@pytest.mark.parametrize("transport", ["thread", "process"])
def test_renewal_during_delivery_preserves_exclusion(tmp_path, monkeypatch, transport):
    import json
    import os
    import subprocess
    import sys
    import cron.jobs as jobs

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0.1)
    job = jobs.create_job(prompt="synthetic", schedule="every 5m")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    owner = claimed["fire_claim"]["by"]
    result = {}
    done = threading.Event()

    def contender():
        if transport == "thread":
            result["renewed"] = jobs.heartbeat_fire_claim(job["id"], expected_owner=owner)
            result["stale_renewed"] = jobs.heartbeat_fire_claim(job["id"], expected_owner="stale")
            result["claimed"] = jobs.claim_job_for_fire(job["id"], claim_ttl_seconds=0)
        else:
            code = '''import json, sys
from cron import jobs
jobs._JOBS_LOCK_TIMEOUT_SECONDS = 0.1
jid, owner = sys.argv[1:]
print(json.dumps(dict(renewed=jobs.heartbeat_fire_claim(jid, expected_owner=owner), stale_renewed=jobs.heartbeat_fire_claim(jid, expected_owner="stale"), claimed=jobs.claim_job_for_fire(jid, claim_ttl_seconds=0))))
'''
            p = subprocess.run([sys.executable, "-c", code, job["id"], owner], env=os.environ.copy(), capture_output=True, text=True, timeout=10)
            result.update(json.loads(p.stdout))
        done.set()

    with jobs.fire_claim_fence(job["id"], expected_owner=owner) as owned:
        assert owned
        t = threading.Thread(target=contender)
        t.start()
        assert done.wait(15)
        assert result == {"renewed": True, "stale_renewed": False, "claimed": False}
        assert jobs.get_job(job["id"])["fire_claim"]["by"] == owner
    t.join(2)
    replacement = jobs.claim_job_for_fire(job["id"], claim_ttl_seconds=0, return_job=True)
    assert isinstance(replacement, dict)
    assert replacement["fire_claim"]["by"] != owner
    assert not jobs.heartbeat_fire_claim(job["id"], expected_owner=owner)
    assert not jobs.mark_job_run(job["id"], True, expected_fire_owner=owner)
    with jobs.fire_claim_fence(job["id"], expected_owner=owner) as owned:
        assert not owned

    # No backend, timeout and I/O error must fail closed, including a nested
    # legacy fallback lock. None may overwrite a replacement's owner token.
    from pathlib import Path
    snapshot = jobs.get_job(job["id"])
    real_acquire = jobs._acquire_flock
    for failure in (False, None, OSError("synthetic unavailable")):
        def acquire(fd, timeout):
            if Path(fd.name).name == ".jobs.lock":
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return real_acquire(fd, timeout)
        monkeypatch.setattr(jobs, "_acquire_flock", acquire)
        new_owner = replacement["fire_claim"]["by"]
        assert not jobs.heartbeat_fire_claim(job["id"], expected_owner=new_owner)
        assert not jobs.claim_job_for_fire(job["id"], claim_ttl_seconds=0)
        assert not jobs.mark_job_run(job["id"], True, expected_fire_owner=new_owner)
        with jobs.fire_claim_fence(job["id"], expected_owner=new_owner) as owned:
            assert not owned
        with jobs._jobs_lock():
            assert not jobs.heartbeat_fire_claim(job["id"], expected_owner=new_owner)
        assert jobs.get_job(job["id"]) == snapshot
    monkeypatch.setattr(jobs, "_acquire_flock", real_acquire)



@pytest.mark.parametrize("outcome", ["success", "delivery_error", "shutdown"])
def test_fenced_delivery_settles_with_live_heartbeat(tmp_path, monkeypatch, outcome):
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from cron.executions import get_execution

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.01)
    job = jobs.create_job(prompt="synthetic", schedule="every 5m", deliver="bot-chat")
    job = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(job, dict)
    delivering = threading.Event()
    renewed = threading.Event()
    cancel = threading.Event()
    real_heartbeat = jobs.heartbeat_fire_claim

    def heartbeat(jid, *, expected_owner):
        result = real_heartbeat(jid, expected_owner=expected_owner)
        if delivering.is_set() and result:
            renewed.set()
        return result

    def delivery(*args, **kwargs):
        delivering.set()
        assert renewed.wait(5), "delivery blocks its own heartbeat"
        if outcome == "shutdown":
            cancel.set()
        return "synthetic delivery failure" if outcome == "delivery_error" else None

    monkeypatch.setattr(scheduler, "heartbeat_fire_claim", heartbeat)
    monkeypatch.setattr(scheduler, "run_job", lambda *a, **k: (True, "fixture", "fixture", None))
    monkeypatch.setattr(scheduler, "_deliver_result", delivery)
    assert scheduler.run_one_job(job, cancel_event=cancel)
    execution = get_execution(job["execution_id"])
    assert isinstance(execution, dict)
    assert renewed.is_set()
    persisted = jobs.get_job(job["id"])
    assert isinstance(persisted, dict)
    assert not persisted.get("fire_claim")
    if outcome == "shutdown":
        assert execution["status"] == "failed"
        assert "Interrupted" in execution["error"]
    else:
        assert execution["status"] == "completed"
        assert execution["delivery_outcome"] == ("failed" if outcome == "delivery_error" else "delivered")
