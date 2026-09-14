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
