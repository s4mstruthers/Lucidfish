"""The coach review is rewritten automatically after new analyses (unless switched off)."""
import time

from lucidfish import jobs, settings, store


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def _fake_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "refresh_player_summary", lambda pid, tc=None: calls.append((pid, tc)) or ("ok", ""))
    return calls


def test_the_setting_is_on_by_default_and_can_be_switched_off():
    assert settings.public_settings()["auto_review"] is True
    settings.save({"auto_review": False})
    assert settings.public_settings()["auto_review"] is False


def test_new_games_lead_to_new_reviews_when_the_queue_is_idle(monkeypatch):
    calls = _fake_refresh(monkeypatch)
    pid = store.create_profile(name="Sam")
    q = jobs.AnalysisQueue()
    try:
        q.note_new_game(pid, "rapid")
        assert _wait(lambda: len(calls) == 2)
        assert calls == [(pid, None), (pid, "rapid")]            # the overall review, then the rapid one
        assert _wait(lambda: q.review_state(pid) == {"pending": False, "running": False, "time_class": ""})
    finally:
        q.shutdown()


def test_switched_off_nothing_is_rewritten(monkeypatch):
    calls = _fake_refresh(monkeypatch)
    pid = store.create_profile(name="Sam")
    settings.save({"auto_review": False})
    q = jobs.AnalysisQueue()
    try:
        q.note_new_game(pid, "blitz")
        time.sleep(0.3)
        assert calls == [] and not q.review_state(pid)["pending"]
    finally:
        q.shutdown()


def test_reviews_still_due_survive_a_restart(monkeypatch):
    calls = _fake_refresh(monkeypatch)
    pid = store.create_profile(name="Sam")
    first = jobs.AnalysisQueue()
    with first._cond:
        first._touched[pid] = {"blitz"}                            # closed before the queue went idle
    first._persist()
    assert first.review_state(pid)["pending"]
    assert store.get_json(jobs._QUEUE_KEY)["reviews_due"] == {str(pid): ["blitz"]}
    second = jobs.AnalysisQueue()
    try:
        second.restore()
        assert _wait(lambda: calls == [(pid, None), (pid, "blitz")])
        assert _wait(lambda: store.get_json(jobs._QUEUE_KEY)["reviews_due"] == {})
    finally:
        second.shutdown()


def test_reviews_remember_when_they_were_written():
    pid = store.create_profile(name="Sam")
    before = time.time()
    store.set_summary(pid, "Overall.")
    store.set_summary(pid, "Rapid.", "rapid")
    times = store.get_profile(pid)["summary_times"]
    assert set(times) == {"", "rapid"} and all(t >= before - 1 for t in times.values())
