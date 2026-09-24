"""Background analysis queue: live progress, learned time estimates, and an
engine head start for the next game.

Games are analysed one at a time. Stockfish already uses every core it is
given, so two games in parallel would each run at half speed and the first
result would arrive later. Instead the queue removes idle time: while the AI
coach is still writing up game N (the engine is finished with it), the engine
searches game N+1's positions into the cache. When N+1 starts, its engine work
is already done. Same depth and settings, so the results are identical.

Time-left estimates come from two sources: what similar analyses took on this
computer before (learned per settings combination) and how fast the current
game is actually going. The live rate takes over as the game progresses.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from collections import deque

from . import ratings, settings, share, store
from .config import Config
from .llm import PROVIDERS, LLMError
from .pipeline import (
    DETAIL_LEVELS,
    analyse_game,
    build_coach,
    parse_game,
    prefetch_engine,
)
from .prompts import build_player_summary_prompt, player_summary_system
from .review_check import tidy_profile_review

_QUEUE_KEY = "queue"
_TIMINGS_KEY = "timings"


# ------------------------------------------------------------------ timings

class Timings:
    """Learns how long analyses take on this computer, per settings combination."""

    ALPHA = 0.35   # weight of the newest game in the running average

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            self._data = store.get_json(_TIMINGS_KEY, {}) or {}
        return self._data

    @staticmethod
    def signature(cfg: Config) -> str:
        coach = f"{cfg.llm.provider}:{cfg.llm.model or 'default'}" if cfg.llm.enabled else "engine-only"
        if cfg.llm.enabled and cfg.expert is not None:
            coach += f"+{cfg.expert.provider}:{cfg.expert.model}"
        return f"d{cfg.engine.depth}|t{cfg.engine.threads}|{coach}|{cfg.analysis.detail}"

    @staticmethod
    def default_guess(cfg: Config) -> tuple[float, float]:
        """(seconds per move, seconds for the review) before anything was measured."""
        engine = 0.2 * 1.35 ** (cfg.engine.depth - 12) * (4 / max(1, cfg.engine.threads)) ** 0.5
        if not cfg.llm.enabled:
            return engine, 0.0
        spec = PROVIDERS.get(cfg.llm.provider)
        local = spec.local if spec else True
        # Commentary windows cover 8 moves per request, so "standard" costs well under a note per move.
        share = {"key": 0.15, "standard": 0.45, "full": 1.0}.get(cfg.analysis.detail, 0.45)
        coach = (5.0 if local else 0.6) * share
        return max(engine, coach), (25.0 if local else 8.0)

    def estimate(self, cfg: Config) -> tuple[float, float, bool]:
        """(seconds per move, review seconds, learned from real runs?)."""
        with self._lock:
            entry = self._load().get(self.signature(cfg))
        if entry:
            return entry["ply"], entry["review"], True
        return (*self.default_guess(cfg), False)

    def record(self, cfg: Config, plies: int, move_seconds: float, review_seconds: float) -> None:
        if plies < 4 or move_seconds <= 0:
            return
        per = move_seconds / plies
        with self._lock:
            data = self._load()
            sig = self.signature(cfg)
            old = data.get(sig)
            if old:
                per = old["ply"] + self.ALPHA * (per - old["ply"])
                review_seconds = old["review"] + self.ALPHA * (review_seconds - old["review"])
            data[sig] = {"ply": round(per, 3), "review": round(review_seconds, 2), "n": (old or {}).get("n", 0) + 1}
            store.set_json(_TIMINGS_KEY, data)


# ------------------------------------------------------------------ jobs

def rating_for_game(pgn: str, side: str | None, given: int | None, profile: dict | None) -> tuple[int | None, str]:
    """The rating to pitch a game's coaching at, and which one it is ("chess.com blitz").

    The game's own rating (its PGN's WhiteElo/BlackElo for the coached side) comes first, then one the game
    list supplied, then the profile's closest rating for that site and time control.
    """
    headers = ratings.pgn_headers(pgn)
    site = ratings.game_site(headers)
    time_class = ratings.time_class(headers)
    own = ratings.game_rating(headers, side) or given
    if own:
        return own, ratings.label(site, time_class)
    stand_in = ratings.for_game((profile or {}).get("ratings") or {}, site, time_class)
    return stand_in if stand_in else (None, "")


class Job:
    """One game waiting for, or going through, analysis. Safe to read while written."""

    def __init__(self, pgn: str, *, side: str | None = None, elo: int | None = None, detail: str | None = None,
                 profile_id: int | None = None, source: str = "single", replace: bool = True):
        game, warnings = parse_game(pgn)       # ValueError for unreadable input
        h = game.headers
        self.id = uuid.uuid4().hex[:12]
        self.pgn = pgn
        self.side = side
        self.elo = elo
        self.detail = detail if detail in DETAIL_LEVELS else None
        self.profile_id = profile_id
        self.source = source                   # "single" (opened by you) or "batch"
        self.replace = replace                 # overwrite a stored analysis of the same game
        self.headers = dict(h)
        self.fingerprint = store.fingerprint(pgn)
        self.title = f"{h.get('White', '?')} vs {h.get('Black', '?')}"
        self.time_class = ratings.time_class(self.headers)   # shown in the queue
        self.total = sum(1 for _ in game.mainline_moves())
        self.lock = threading.Lock()
        self.status = "queued"                 # queued → running → done | stopped | error | cancelled
        self.label = "Waiting in the queue…"
        self.engine_done = 0
        self.prefetched = 0                    # positions searched ahead of time
        self.moves: list[dict] = []
        self.review = ""
        self.chapters: list[dict] = []
        self.error = ""
        self.warnings = list(warnings)
        self.accuracy: dict = {}
        self.opening = ""
        self.coach = ""
        self.game_id: int | None = None
        self.stop = False
        self.created = time.time()
        self.started: float | None = None
        self.review_started: float | None = None
        self.finished: float | None = None
        self.per_ply = 0.0                     # estimate used for the time-left figures
        self.review_s = 0.0
        self.coach_on = False
        self.learned = False

    def record(self) -> dict:
        """What is persisted so the queue survives a restart."""
        return {"pgn": self.pgn, "side": self.side, "elo": self.elo, "detail": self.detail,
                "profile_id": self.profile_id, "source": self.source, "replace": self.replace}

    def progress(self, stage: str, done: int, total: int, label: str) -> None:
        with self.lock:
            if stage == "engine":
                self.engine_done = done
            elif stage == "review":            # chapters, then the review: the "writing up" stage
                self.review_started = self.review_started or time.time()
                self.label = label or "Writing the post-game review…"
                return
            self.label = label

    def add_move(self, move) -> None:
        data = move.to_dict()
        with self.lock:
            self.moves.append(data)

    def remaining(self, now: float) -> float:
        """Seconds until this job is finished (running jobs use their live speed)."""
        if self.status != "running" or self.started is None:
            return self.total * self.per_ply + (self.review_s if self.coach_on else 0.0)
        if self.review_started:
            return max(2.0, self.review_s - (now - self.review_started))
        elapsed = now - self.started
        done = len(self.moves)
        prior = max(0.0, self.total * self.per_ply - elapsed)
        if done >= 3:
            observed = (self.total - done) * elapsed / done
            weight = min(1.0, done / 12)
            left = weight * observed + (1 - weight) * prior
        else:
            left = prior
        return left + (self.review_s if self.coach_on else 0.0)

    def user_accuracy(self):
        if not self.accuracy:
            return None
        if self.side:
            return self.accuracy.get(self.side)
        vals = [v for v in self.accuracy.values() if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def summary(self, now: float, eta: float | None = None, start_in: float | None = None) -> dict:
        with self.lock:
            return {
                "id": self.id, "title": self.title, "date": self.headers.get("Date", ""), "side": self.side,
                "time_class": self.time_class,
                "source": self.source, "detail": self.detail, "status": self.status, "label": self.label,
                "total": self.total, "done": len(self.moves), "engine_done": self.engine_done,
                "prefetched": self.prefetched, "eta_s": None if eta is None else round(eta, 1),
                "start_in_s": None if start_in is None else round(start_in, 1),
                "elapsed_s": round(now - self.started, 1) if self.started and not self.finished else None,
                "duration_s": round(self.finished - self.started, 1) if self.started and self.finished else None,
                "accuracy": self.user_accuracy(), "game_id": self.game_id, "error": self.error,
                "learned": self.learned, "reviewing": bool(self.review_started and not self.finished),
            }

    def snapshot(self, since: int = 0) -> dict:
        """Full state for the game page (only moves it hasn't seen yet)."""
        with self.lock:
            return {
                "id": self.id, "status": self.status, "total": self.total, "headers": self.headers,
                "side": self.side, "engine_done": self.engine_done, "done": len(self.moves),
                "label": self.label, "moves": self.moves[since:], "since": since, "review": self.review,
                "chapters": self.chapters,
                "error": self.error, "warnings": self.warnings, "accuracy": self.accuracy,
                "opening": self.opening, "coach": self.coach, "game_id": self.game_id,
            }


# ------------------------------------------------------------------ queue

class AnalysisQueue:
    """A reorderable queue processed by one background worker."""

    FINISHED_KEPT = 40

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._queued: list[Job] = []
        self._running: Job | None = None
        self._finished: deque[Job] = deque()
        self._jobs: dict[str, Job] = {}
        self._session: list[str] = []          # jobs since the queue was last idle (overall progress)
        # Profiles with new games, and their time controls: their coach reviews are rewritten when the queue is
        # idle (never slowing an analysis down). Remembered across restarts.
        self._touched: dict[int, set[str]] = {}
        self._refreshing: dict[int, set[str]] = {}
        self._reviewing: dict | None = None      # the review being written right now
        self._worker: threading.Thread | None = None
        self._prefetch: tuple[threading.Thread, threading.Event] | None = None
        self.paused = False
        self.restored = 0
        self.timings = Timings()
        self._closed = False

    # ---------------------------------------------------------- public API

    @property
    def busy(self) -> bool:
        return self._running is not None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def find_pending(self, pgn: str) -> Job | None:
        """The running or queued job for this exact game, if any (avoids analysing it twice)."""
        fp = store.fingerprint(pgn)
        with self._cond:
            return next((j for j in ([self._running] if self._running else []) + self._queued
                         if j.fingerprint == fp and not j.stop), None)

    def add(self, jobs: list[Job], front: bool = False) -> None:
        with self._cond:
            if self._running is None and not self._queued:
                self._session = []   # the queue was idle: overall progress starts afresh
            for job in jobs:
                self._jobs[job.id] = job
                self._session.append(job.id)
            if front:
                self._queued[0:0] = jobs
            else:
                self._queued.extend(jobs)
            self._cond.notify_all()
        self._persist()
        self._ensure_worker()

    def move(self, job_id: str, where: str) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if job not in self._queued:
                return False
            i = self._queued.index(job)
            self._queued.pop(i)
            target = {"top": 0, "up": max(0, i - 1), "down": i + 1, "bottom": len(self._queued)}.get(where, i)
            self._queued.insert(min(target, len(self._queued)), job)
            self._cond.notify_all()
        self._persist()
        return True

    def cancel(self, job_id: str) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job is self._running:
                job.stop = True
                job.label = "Stopping…"
                return True
            if job not in self._queued:
                return False
            self._queued.remove(job)
            self._finish(job, "cancelled")
        self._persist()
        return True

    def pause(self) -> None:
        with self._cond:
            self.paused = True
        self._persist()

    def resume(self) -> None:
        with self._cond:
            self.paused = False
            self.restored = 0
            self._cond.notify_all()
        self._persist()
        self._ensure_worker()

    def stop_all(self) -> None:
        with self._cond:
            for job in list(self._queued):
                self._finish(job, "cancelled")
            self._queued.clear()
            if self._running:
                self._running.stop = True
                self._running.label = "Stopping…"
            self.restored = 0
        self._persist()

    def clear_finished(self) -> None:
        with self._cond:
            for job in self._finished:
                self._jobs.pop(job.id, None)
            self._finished.clear()
            self._session = [jid for jid in self._session if jid in self._jobs]

    def shutdown(self) -> None:
        """Server stopping: remember the queue (the running game included), stop the worker."""
        self._persist()
        with self._cond:
            self._closed = True
            if self._running:
                self._running.stop = True
            self._cond.notify_all()
        self._stop_prefetch()
        if self._worker:
            self._worker.join(timeout=10)

    def restore(self) -> int:
        """Re-queue games left over from the last run (paused until you resume)."""
        saved = store.get_json(_QUEUE_KEY, {}) or {}
        jobs = []
        for rec in saved.get("jobs", []):
            try:
                jobs.append(Job(**rec))
            except (TypeError, ValueError):
                continue
        due = {int(pid): set(classes) for pid, classes in (saved.get("reviews_due") or {}).items()
               if str(pid).isdigit() and store.get_profile(int(pid))}
        if jobs:
            with self._cond:
                self.paused = True
                self.restored = len(jobs)
                self._touched.update(due)
            self.add(jobs)
        elif due:
            with self._cond:
                self._touched.update(due)
            self._ensure_worker()     # reviews left from last time: write them now
        return len(jobs)

    def snapshot(self) -> dict:
        now = time.time()
        base = settings.load_config()
        with self._cond:
            running, queued, finished = self._running, list(self._queued), list(self._finished)
            session = [self._jobs[j] for j in self._session if j in self._jobs]
            paused, restored = self.paused, self.restored
        for job in queued:
            self._apply_estimate(job, base)
        clock = running.remaining(now) if running else 0.0
        run_view = running.summary(now, eta=clock) if running else None
        queue_view = []
        for job in queued:
            took = job.remaining(now)
            queue_view.append(job.summary(now, eta=took, start_in=clock))
            clock += took
        counted = [j for j in session if j.status != "cancelled"]
        total_plies = sum(j.total for j in counted) or 1
        done_plies = sum(j.total if j.status in ("done", "stopped", "error") else len(j.moves) for j in counted)
        learned = all(j.learned for j in ([running] if running else []) + queued)
        return {
            "paused": paused, "restored": restored, "busy": running is not None, "reviewing": self._reviewing,
            "running": run_view, "queued": queue_view,
            "finished": [j.summary(now) for j in finished],
            "totals": {
                "games_left": len(queued) + (1 if running else 0),
                "games_done": sum(1 for j in counted if j.status in ("done", "stopped", "error")),
                "games_total": len(counted),
                "progress": round(done_plies / total_plies, 4),
                "seconds_left": round(clock, 1),
                "finish_at": now + clock if (running or queued) else None,
                "learned": learned,
            },
        }

    def job_view(self, job_id: str, since: int = 0) -> dict | None:
        """A job's full state plus where it stands in the queue."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        view = job.snapshot(since)
        now = time.time()
        with self._cond:
            queued = list(self._queued)
            running = self._running
        if job is running:
            view["eta_s"] = round(job.remaining(now), 1)
        elif job in queued:
            base = settings.load_config()
            wait = running.remaining(now) if running else 0.0
            for other in queued[:queued.index(job)]:
                self._apply_estimate(other, base)
                wait += other.remaining(now)
            self._apply_estimate(job, base)
            view.update(position=queued.index(job) + 1, ahead=queued.index(job) + (1 if running else 0),
                        start_in_s=round(wait, 1), eta_s=round(wait + job.remaining(now), 1),
                        paused=self.paused, prefetched=job.prefetched)
        return view

    # ---------------------------------------------------------- worker

    def _ensure_worker(self) -> None:
        with self._cond:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="lucidfish-queue", daemon=True)
                self._worker.start()

    def _loop(self) -> None:
        while not self._closed:
            job, refresh = None, {}
            with self._cond:
                while not self._closed:
                    if self._queued and not self.paused:
                        job = self._queued.pop(0)
                        job.status, job.started, job.label = "running", time.time(), "Starting the engine…"
                        self._running = job
                        break
                    if self._touched and (not self._queued or self.paused):
                        refresh, self._touched = dict(self._touched), {}
                        self._refreshing = refresh
                        break
                    self._cond.wait(timeout=5)
            if job is None:
                self._write_reviews(refresh)
                continue
            self._persist()
            try:
                self._run(job)
            except Exception as e:   # surface any failure instead of killing the worker
                with job.lock:
                    job.error = str(e)
                    job.status = "error"
            finally:
                self._stop_prefetch()
                with self._cond:
                    self._running = None
                    self._finish(job, job.status if job.status != "running" else "done")
                    self._cond.notify_all()
                self._persist()

    def _run(self, job: Job) -> None:
        cfg = self._config_for(job)
        self._apply_estimate(job, cfg, exact=True)
        profile = store.get_profile(job.profile_id) if job.profile_id else None
        job.elo, cfg.user_elo_label = rating_for_game(job.pgn, job.side, job.elo, profile)
        cfg.user_elo = job.elo
        cache = store.EngineCache()

        def progress(stage: str, done: int, total: int, label: str) -> None:
            job.progress(stage, done, total, label)
            if stage == "engine" and done >= total:
                self._start_prefetch(cfg, cache)   # engine idle from here: give the next game a head start

        report = analyse_game(job.pgn, cfg, side_filter=job.side, progress=progress, on_move=job.add_move,
                              should_stop=lambda: job.stop,
                              player_context=coach_context(profile, job.pgn),
                              level=(profile or {}).get("level") or None, engine_cache=cache)
        end = time.time()
        with job.lock:
            job.review, job.accuracy, job.opening, job.coach = (report.review, report.accuracy,
                                                                 report.opening, report.coach)
            job.chapters = report.chapters
            job.warnings = report.warnings
            moves = list(job.moves)
            completed = not job.stop
            if completed:
                job.label = "Saving…"
        game_id = None
        if completed:
            review_start = job.review_started or end
            self.timings.record(cfg, job.total, review_start - job.started, end - review_start)
            if profile:
                game_id = store.save_game(profile["id"], job.pgn, report.headers, job.side, job.elo,
                                          report.opening, report.review, moves, report.time_class,
                                          report.accuracy, replace=job.replace, chapters=report.chapters)
                if game_id and cfg.llm.enabled:
                    self.note_new_game(profile["id"], report.time_class)
                if game_id:
                    share.sync_profile(profile["id"])   # keep a shared copy up to date, if enabled
        # Only now is the result final: a page that sees "done" can rely on the game being saved.
        with job.lock:
            job.game_id = game_id
            job.status = "done" if completed else "stopped"

    # ---------------------------------------------------------- prefetch

    def _start_prefetch(self, cfg: Config, cache) -> None:
        if not cfg.llm.enabled or not store.get_settings().get("prefetch", True):
            return   # engine-only: the engine is the bottleneck anyway, nothing to overlap
        with self._cond:
            if self.paused or not self._queued or self._prefetch is not None:
                return
            nxt = self._queued[0]
        if nxt.prefetched > nxt.total:
            return
        # Half the threads: the coach may be running on this CPU too.
        pcfg = dataclasses.replace(cfg, engine=dataclasses.replace(
            cfg.engine, threads=max(1, cfg.engine.threads // 2)))
        stop = threading.Event()

        def still_next() -> bool:
            with self._cond:
                return bool(self._queued) and self._queued[0] is nxt and not self.paused

        def run() -> None:
            try:
                prefetch_engine(nxt.pgn, pcfg, cache, should_stop=lambda: stop.is_set() or not still_next(),
                                on_position=lambda d, t: setattr(nxt, "prefetched", d))
            except Exception:
                pass   # a head start is optional; the real run will do the work

        thread = threading.Thread(target=run, name="lucidfish-prefetch", daemon=True)
        self._prefetch = (thread, stop)
        thread.start()

    def _stop_prefetch(self) -> None:
        if self._prefetch:
            thread, stop = self._prefetch
            stop.set()
            thread.join(timeout=30)   # finishes the position it is on; never two engines at once
            self._prefetch = None

    # ---------------------------------------------------------- helpers

    def _config_for(self, job: Job, base: Config | None = None) -> Config:
        cfg = base or settings.load_config()
        if job.detail:
            cfg = dataclasses.replace(cfg, analysis=dataclasses.replace(cfg.analysis, detail=job.detail))
        cfg.user_elo = job.elo
        return cfg

    def _apply_estimate(self, job: Job, cfg: Config, exact: bool = False) -> None:
        if job.started and not exact:
            return
        cfg = cfg if exact else self._config_for(job, cfg)
        job.per_ply, job.review_s, job.learned = self.timings.estimate(cfg)
        job.coach_on = cfg.llm.enabled

    def _finish(self, job: Job, status: str) -> None:
        """Move a job to the finished list (caller holds the lock)."""
        job.status = status
        job.finished = job.finished or time.time()
        if status == "cancelled":
            job.label = "Removed from the queue"
        self._finished.appendleft(job)
        while len(self._finished) > self.FINISHED_KEPT:
            old = self._finished.pop()
            if old.id not in self._session:
                self._jobs.pop(old.id, None)

    # ---------------------------------------------------------- coach reviews

    def note_new_game(self, pid: int, time_class: str) -> None:
        """A game was saved: rewrite the profile's reviews once the queue is idle (if that's switched on)."""
        if not store.get_settings().get("auto_review", True):
            return
        with self._cond:
            self._touched.setdefault(pid, set()).add(time_class or "")
            self._cond.notify_all()
        self._persist()
        self._ensure_worker()

    def _write_reviews(self, refresh: dict[int, set[str]]) -> None:
        try:
            for pid, classes in refresh.items():
                for tc in [None, *sorted(c for c in classes if c)]:
                    with self._cond:
                        self._reviewing = {"profile_id": pid, "time_class": tc or ""}
                    refresh_player_summary(pid, tc)
        finally:
            with self._cond:
                self._reviewing, self._refreshing = None, {}
            self._persist()

    def review_state(self, pid: int | None) -> dict:
        """For the Home page: is this profile's review waiting to be rewritten, or being written?"""
        with self._cond:
            running = self._reviewing if self._reviewing and self._reviewing["profile_id"] == pid else None
            pending = pid in self._touched or pid in self._refreshing
        return {"pending": pending, "running": bool(running), "time_class": running["time_class"] if running else ""}

    def _persist(self) -> None:
        if self._closed:
            return   # shutting down: keep the state saved by shutdown()
        with self._cond:
            pending = ([self._running] if self._running and not self._running.stop else []) + self._queued
            due: dict[str, list[str]] = {}
            for src in (self._refreshing, self._touched):
                for pid, classes in src.items():
                    due[str(pid)] = sorted(set(due.get(str(pid), [])) | classes)
            state = {"paused": self.paused, "jobs": [j.record() for j in pending], "reviews_due": due}
        try:
            store.set_json(_QUEUE_KEY, state)
        except Exception:
            pass   # persistence is a convenience; never break the queue over it


def coach_context(profile: dict | None, pgn: str) -> str:
    """What the coach knows about the player when analysing a game: the review of their games in that time
    control if there is one (lessons differ between bullet and classical), else the overall review."""
    if not profile:
        return ""
    tc = ratings.time_class(ratings.pgn_headers(pgn))
    return (profile.get("summaries") or {}).get(tc) or profile.get("summary") or ""


def refresh_player_summary(pid: int, time_class: str | None = None) -> tuple[str, str]:
    """Re-write a profile's coach review (of all games, or of one time control) from its stats and recent
    reviews. Returns (summary, error)."""
    stats = store.aggregate_stats(pid, time_class=time_class)
    if stats.get("games", 0) < 2:
        return "", f"Analyse at least two {time_class + ' ' if time_class else ''}games first."
    coach, warnings = build_coach(settings.load_config())
    if not coach.available:
        return "", warnings[0] if warnings else "The AI coach is turned off in Settings."
    prompt = build_player_summary_prompt(stats, store.recent_reviews(pid, time_class=time_class),
                                         profile=store.get_profile(pid), time_class=time_class)
    try:
        raw = coach.hard(lambda llm: llm.generate(player_summary_system(time_class), prompt))
        summary = tidy_profile_review(raw, stats, time_class)   # the fixed structure, whatever the model did
    except LLMError as e:
        return "", str(e)
    store.set_summary(pid, summary, time_class)
    share.sync_profile(pid)
    return summary, ""
