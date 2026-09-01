from enum import Enum
from typing import Optional
from build.sandbox import BuildResult
from safety.scope_guard import ScopeViolationError
from safety.patch_validator import PatchValidationError


class FailureType(str, Enum):
    ENV_TRANSIENT = "ENV_TRANSIENT"
    PATCH_INVALID = "PATCH_INVALID"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    SYNTAX = "SYNTAX"
    TYPE_SEMANTIC = "TYPE_SEMANTIC"
    BEHAVIORAL = "BEHAVIORAL"
    UNKNOWN = "UNKNOWN"


class FailureClassifier:
    @classmethod
    def classify_exception(cls, exc: Exception) -> FailureType:
        if isinstance(exc, ScopeViolationError):
            return FailureType.SCOPE_VIOLATION
        if isinstance(exc, (PatchValidationError, ValueError)):
            return FailureType.PATCH_INVALID
        return FailureType.UNKNOWN

    @classmethod
    def classify_build_failure(cls, build_result: BuildResult) -> FailureType:
        if build_result.success:
            return FailureType.UNKNOWN

        combined_error = " ".join([e.message for e in build_result.errors]).lower()
        raw = build_result.raw_output.lower()

        if any(marker in combined_error or marker in raw for marker in ["cs1002", "cs1513", "syntax error", "unexpected token"]):
            return FailureType.SYNTAX

        if any(marker in combined_error or marker in raw for marker in ["cs0246", "cs1503", "cs0103", "cannot find symbol", "type mismatch", "is not defined"]):
            return FailureType.TYPE_SEMANTIC

        if any(marker in raw for marker in ["timed out", "connection refused", "econnreset", "killed"]):
            return FailureType.ENV_TRANSIENT

        return FailureType.UNKNOWN
