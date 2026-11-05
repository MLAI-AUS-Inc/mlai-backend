"""Safe provider failure taxonomy; never expose upstream response bodies."""


class CalendarError(Exception):
    """An expected connection/provider failure safe to classify for a member."""

    def __init__(self, code, *, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)
