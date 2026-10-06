class RkaiError(Exception):
    """Stable machine code, safe public message, and CLI exit status."""

    def __init__(self, code: str, message: str, exit_code: int = 1, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details

    def result(self) -> dict:
        error = {"code": self.code, "message": str(self), "exit_code": self.exit_code}
        if self.details:
            error["details"] = self.details
        return {"error": error}


def database_error(error):
    import sqlite3

    busy = isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower()
    return RkaiError(
        "busy" if busy else "database_error",
        "Library database is busy" if busy else "Library database operation failed",
        4 if busy else 1,
    )
