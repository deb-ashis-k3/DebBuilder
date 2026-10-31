"""Engine errors that cross module boundaries (API handlers map them to HTTP responses)."""


class Overloaded(RuntimeError):
    """Too many executions are already waiting for a slot (MAX_QUEUED_EXECUTIONS): refuse instead of queueing.
    HTTP handlers answer 503 with Retry-After."""

    def __init__(self, message: str = "Server is busy: too many executions are queued. Retry shortly.", retry_after: int = 2):
        super().__init__(message)
        self.retry_after = retry_after


class ExecutionTimeout(RuntimeError):
    """The workflow ran longer than its execution timeout."""
