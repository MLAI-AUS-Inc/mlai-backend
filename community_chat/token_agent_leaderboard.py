"""Rank coding agents using the same public cohort as the people leaderboard."""

SOURCE_LABELS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "pi": "Pi",
}


def agent_leaderboard(entries):
    """Fold normalized source totals from public entries, before pagination.

    Callers must exclude private accounts, including a hidden caller's ``you``
    row. A participant is one leaderboard identity with positive usage for an
    agent; the same person can contribute to several agents.
    """
    agents = {}
    for entry in entries:
        seen_sources = set()
        for usage in entry.get("source_totals", []):
            source = usage["source"]
            if usage["grand_total"] <= 0:
                continue
            agent = agents.setdefault(source, {
                "source": source,
                "display_name": SOURCE_LABELS.get(
                    source, source.replace("_", " ").replace("-", " ").title()
                ),
                "grand_total": 0,
                "sessions": 0,
                "participants": 0,
            })
            agent["grand_total"] += usage["grand_total"]
            agent["sessions"] += usage["sessions"]
            if source not in seen_sources:
                agent["participants"] += 1
                seen_sources.add(source)
    ranked = sorted(agents.values(), key=lambda row: (-row["grand_total"], row["source"]))
    return [dict(row, rank=rank) for rank, row in enumerate(ranked, start=1)]
