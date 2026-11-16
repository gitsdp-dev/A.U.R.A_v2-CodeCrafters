"""Central user-visible strings for CAD cache controls and summaries."""

CAD_CACHE_TITLE = "CAD PART CACHE"
CAD_CACHE_ENABLED_LABEL = "Reuse saved parts"
CAD_CACHE_ENTRY_COUNT_LABEL = "Saved parts"
CAD_CACHE_SIZE_LABEL = "Cache size"
CAD_CACHE_CLEAR_ACTION = "Clear part cache"
CAD_CACHE_REFRESH_ACTION = "Refresh cache stats"
CAD_CACHE_CLEAR_CONFIRM_TITLE = "Clear CAD part cache?"
CAD_CACHE_CLEAR_CONFIRM_DETAIL = "This removes saved reusable CAD parts from this device."
CAD_CACHE_CLEARED = "CAD part cache cleared."
CAD_CACHE_CLEAR_FAILED = "Could not clear CAD part cache."
CAD_CACHE_LOADING = "Loading…"
CAD_CACHE_ENABLED = "CAD part cache is on."
CAD_CACHE_DISABLED = "CAD part cache is off."
CAD_CACHE_SETTING_FAILED = "Could not save CAD cache setting."
CAD_CACHE_OTHER_CONFIRMATION = "Another confirmation is already open."


def cache_summary(reused: int, generated: int) -> str:
    if reused <= 0:
        return ""
    part_word = "part" if reused == 1 else "parts"
    new_word = "one" if generated == 1 else "ones"
    return f"Reused {reused} saved {part_word}, generated {generated} new {new_word}."


def cache_size_label(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def cache_status(entry_count: int, size_bytes: int) -> str:
    return f"{entry_count} saved parts · {cache_size_label(size_bytes)}"
