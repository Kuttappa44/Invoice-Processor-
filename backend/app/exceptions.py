"""Shared error types. HTTP handlers will register here in a later phase."""


class ProcessCancelled(Exception):
    """Raised when the API user requests cancellation; stops between email iterations."""
