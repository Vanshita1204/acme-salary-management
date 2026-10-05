"""Service-layer rule violations, mapped to HTTP status codes by the API."""

from enum import Enum


class Problem(Enum):
    NOT_FOUND = 404
    CONFLICT = 409
    TOO_LARGE = 413
    INVALID = 422


class ServiceError(Exception):
    def __init__(self, problem: Problem, message: str) -> None:
        super().__init__(message)
        self.problem = problem
        self.message = message
