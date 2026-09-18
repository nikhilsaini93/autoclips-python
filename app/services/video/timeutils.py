def time_to_seconds(time_str) -> float:
    """Accepts 'HH:MM:SS', 'MM:SS', or a plain number of seconds."""
    if isinstance(time_str, (int, float)):
        v = float(time_str)
        if v != v or v in (float("inf"), float("-inf")) or v < 0:
            raise ValueError(f"invalid time value: {time_str!r}")
        return v
    s = str(time_str).strip()
    if not s:
        raise ValueError(f"invalid time value: {time_str!r}")
    # Plain seconds (also covers "90", "90.5").
    if ":" not in s:
        try:
            v = float(s)
        except ValueError:
            raise ValueError(f"invalid time value: {time_str!r}")
        if v != v or v in (float("inf"), float("-inf")) or v < 0:
            raise ValueError(f"invalid time value: {time_str!r}")
        return v
    parts = s.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid time value: {time_str!r}")
    try:
        nums = [float(p.strip()) for p in parts]
    except ValueError:
        raise ValueError(f"invalid time value: {time_str!r}")
    for v in nums:
        if v != v or v in (float("inf"), float("-inf")) or v < 0:
            raise ValueError(f"invalid time value: {time_str!r}")
    if len(nums) == 3:
        h, m, s_ = nums
        return h * 3600 + m * 60 + s_
    m, s_ = nums
    return m * 60 + s_


def snap_to_silence(start_sec: float, end_sec: float, words: list | None = None,
                    silences: list | None = None, segments: list | None = None,
                    window_sec: float = 0.8, duration_sec: float | None = None) -> tuple:
    """Snaps cut boundaries to word/sentence/silence edges so clips never start
    or end mid-word.

    - Each boundary searches +-window_sec for the nearest word edge, then
      prefers sentence ends (segments) and silence midpoints inside the window.
    - Clamps to [0, duration_sec] when known; guarantees end > start by
      falling back to the raw values when snapping would invert the range.
    Returns (snapped_start, snapped_end)."""
    words = words or []
    silences = silences or []
    segments = segments or []

    # Pre-extract sorted edges once (was O(words*candidates) linear scan per
    # boundary — slow on 10k-word videos).
    word_edges: list[float] = []
    for w in words:
        for k in ("start", "end"):
            try:
                word_edges.append(float(w[k]))
            except (KeyError, TypeError, ValueError):
                continue
    word_edges.sort()
    seg_edges: list[float] = []
    for seg in segments:
        for k in ("start", "end"):
            try:
                seg_edges.append(float(seg[k]))
            except (KeyError, TypeError, ValueError):
                continue
    seg_edges.sort()
    silence_spans: list[tuple[float, float, float]] = []
    for s in silences:
        try:
            ss, se = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if se > ss:
            silence_spans.append((ss, se, (ss + se) / 2.0))

    import bisect

    def _nearest_sorted(t: float, edges: list[float]) -> float:
        if not edges:
            return t
        i = bisect.bisect_left(edges, t)
        best, best_d = t, window_sec + 1e-9
        for j in (i - 1, i):
            if 0 <= j < len(edges):
                d = abs(edges[j] - t)
                if d < best_d:
                    best, best_d = edges[j], d
        return best if best_d <= window_sec else t

    def _nearest_word_edge(t: float) -> float:
        return _nearest_sorted(t, word_edges)

    def _prefer_sentence_or_silence(t: float, edge: float) -> float:
        # Sentence ends win: a boundary near a sentence end should sit exactly there.
        best, best_d = edge, abs(edge - t)
        cand = _nearest_sorted(t, seg_edges)
        if cand != t and abs(cand - t) <= window_sec and abs(cand - t) < best_d:
            best, best_d = cand, abs(cand - t)
        # Silences win over raw word edges: sit in the middle of the pause —
        # but only when the pause is within the snap window (old code jumped
        # up to 5s into a long music break ignoring window_sec).
        for ss, se, mid in silence_spans:
            if abs(mid - t) <= window_sec and abs(mid - t) < best_d:
                best, best_d = mid, abs(mid - t)
            # Boundary already inside a pause: center it, but only if the
            # pause itself is near (within window of t) to avoid huge jumps.
            if ss - 1e-6 <= t <= se + 1e-6 and (se - ss) / 2.0 <= window_sec + 1.0:
                return mid
        return best

    ns = _prefer_sentence_or_silence(start_sec, _nearest_word_edge(start_sec))
    ne = _prefer_sentence_or_silence(end_sec, _nearest_word_edge(end_sec))
    if duration_sec is not None:
        try:
            duration_sec = float(duration_sec)
            ns = max(0.0, min(ns, duration_sec))
            ne = max(0.0, min(ne, duration_sec))
        except (TypeError, ValueError):
            pass
    else:
        ns = max(0.0, ns)
        ne = max(0.0, ne)
    if ne <= ns:
        return start_sec, end_sec
    return ns, ne


def seconds_to_time(seconds: float) -> str:
    """Formats seconds as HH:MM:SS.mmm for writing snapped times back."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
