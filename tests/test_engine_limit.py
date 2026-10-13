# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""Every depth-limited search also carries a time cap: a forced mate otherwise keeps Stockfish busy for an hour."""
from lucidfish.config import EngineConfig
from lucidfish.engine import EngineAnalyser


def _analyser(**cfg):
    a = EngineAnalyser.__new__(EngineAnalyser)      # no Stockfish process needed for the limit itself
    a.cfg = EngineConfig(path="stockfish", **cfg)
    return a


def test_depth_searches_are_time_capped():
    limit, kind, strength = _analyser(depth=18, max_seconds=30)._limit()
    assert (limit.depth, limit.time, kind, strength) == (18, 30, "depth", 18)
    deeper, _, strength = _analyser(depth=18, max_seconds=30)._limit(extra_depth=4)
    assert (deeper.depth, deeper.time, strength) == (22, 90, 22)        # the double-check may take longer
    uncapped, _, _ = _analyser(depth=18, max_seconds=0)._limit()
    assert uncapped.time is None
    timed, kind, _ = _analyser(movetime_s=2.0)._limit()
    assert (timed.time, timed.depth, kind) == (2.0, None, "time")
