def time_to_seconds(time_str) -> float:
    """Accepts 'HH:MM:SS', 'MM:SS', or a plain number of seconds."""
    if isinstance(time_str, (int, float)):
        return float(time_str)
    parts = [float(p) for p in str(time_str).split(":")]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    return parts[0]


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

    def _nearest_word_edge(t: float) -> float:
        best, best_d = t, window_sec + 1e-9
        for w in words:
            for k in ("start", "end"):
                try:
                    e = float(w[k])
                except (KeyError, TypeError, ValueError):
                    continue
                d = abs(e - t)
                if d < best_d:
                    best, best_d = e, d
        return best if best_d <= window_sec else t

    def _prefer_sentence_or_silence(t: float, edge: float) -> float:
        # Sentence ends win: a boundary near a sentence end should sit exactly there.
        best, best_d = edge, abs(edge - t)
        for seg in segments:
            for k in ("start", "end"):
                try:
                    e = float(seg[k])
                except (KeyError, TypeError, ValueError):
                    continue
                d = abs(e - t)
                if d <= window_sec and d < best_d:
                    best, best_d = e, d
        # Silences win over raw word edges: sit in the middle of the pause.
        for s in silences:
            try:
                ss, se = float(s["start"]), float(s["end"])
            except (KeyError, TypeError, ValueError):
                continue
            mid = (ss + se) / 2.0
            if abs(mid - t) <= window_sec and abs(mid - t) < best_d:
                best, best_d = mid, abs(mid - t)
            # Boundary sitting inside a pause already: center it.
            if ss - 1e-6 <= t <= se + 1e-6:
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
