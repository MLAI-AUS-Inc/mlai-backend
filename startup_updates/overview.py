"""Keep the founder's own "what we do" introduction instead of regenerating it."""


def founder_overview(memo, run_request, previous_memo=None):
    """Pin a generated draft's ``startupOverview`` to text the founder chose.

    Generation may only fill the introduction when the founder has none. The
    order is: the overview frozen on the run (including a deliberate blank),
    then the update's saved overview, then the startup's own short
    description from when it was added. Otherwise the generated text stays.
    """
    request = run_request or {}
    chosen = request.get("startup_overview")
    if not isinstance(chosen, str):
        saved = (previous_memo or {}).get("startupOverview")
        profile = (request.get("startup_context") or {}).get("short_description")
        chosen = next((value for value in (saved, profile) if isinstance(value, str) and value.strip()), None)
    if chosen is None:
        return memo
    return {**memo, "startupOverview": chosen.strip()}
