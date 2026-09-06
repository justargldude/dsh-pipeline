from enum import Enum
from typing import Optional
from build.sandbox import BuildResult
from safety.scope_guard import ScopeViolationError
from safety.patch_validator import PatchValidationError
from core.workspace import WorktreeStagingError, WorktreeCleanupError


class FailureType(str, Enum):
    # Provider failures
    MODEL_FORMAT_ERROR = "MODEL_FORMAT_ERROR"
    MODEL_CONTENT_INVALID = "MODEL_CONTENT_INVALID"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK_ERROR = "NETWORK_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"

    # Pipeline / Safety / Execution failures
    ENV_TRANSIENT = "ENV_TRANSIENT"
    PATCH_INVALID = "PATCH_INVALID"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    SYNTAX = "SYNTAX"
    TYPE_SEMANTIC = "TYPE_SEMANTIC"
    BEHAVIORAL = "BEHAVIORAL"
    MUTATION_COVERAGE = "MUTATION_COVERAGE"
    CONFIG_MISSING = "CONFIG_MISSING"
    BUILD_FAILED = "BUILD_FAILED"
    REGRESSION = "REGRESSION"
    PROCESS_FAILURE = "PROCESS_FAILURE"
    VALIDATOR_UNAVAILABLE = "VALIDATOR_UNAVAILABLE"
    TRANSACTION_INTEGRITY_FAILURE = "TRANSACTION_INTEGRITY_FAILURE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    UNKNOWN_STATE = "UNKNOWN_STATE"
    HARD_STOP = "HARD_STOP"
    HARD_HALT = "HARD_HALT"
    UNKNOWN = "UNKNOWN"


class FailureClassifier:
    HARD_STOP_TYPES = {
        FailureType.SCOPE_VIOLATION,
        FailureType.AUTH_ERROR,
        FailureType.CONFIG_MISSING,
        FailureType.TRANSACTION_INTEGRITY_FAILURE,
        FailureType.ROLLBACK_FAILED,
        FailureType.UNKNOWN_STATE,
        FailureType.HARD_STOP,
    }

    TRANSIENT_TYPES = {
        FailureType.RATE_LIMIT,
        FailureType.NETWORK_ERROR,
        FailureType.SERVER_ERROR,
        FailureType.TIMEOUT,
        FailureType.ENV_TRANSIENT,
    }

    @classmethod
    def is_hard_stop(cls, failure_type: FailureType) -> bool:
        """Determines if a failure cannot and must not be retried."""
        return failure_type in cls.HARD_STOP_TYPES

    @classmethod
    def is_transient(cls, failure_type: FailureType) -> bool:
        """Determines if a failure is transient at the network/provider layer."""
        return failure_type in cls.TRANSIENT_TYPES

    @classmethod
    def classify_exception(cls, exc: Exception) -> FailureType:
        if isinstance(exc, ScopeViolationError):
            return FailureType.SCOPE_VIOLATION
        if isinstance(exc, WorktreeStagingError):
            return FailureType.TRANSACTION_INTEGRITY_FAILURE
        if isinstance(exc, WorktreeCleanupError):
            return FailureType.ROLLBACK_FAILED
        if isinstance(exc, (PatchValidationError, ValueError)):
            return FailureType.PATCH_INVALID
        return FailureType.UNKNOWN

    @classmethod
    def classify_build_failure(cls, build_result: BuildResult) -> FailureType:
        if build_result.success:
            return FailureType.UNKNOWN

        if getattr(build_result, "timed_out", False):
            return FailureType.TIMEOUT

        combined_error = " ".join([e.message for e in build_result.errors]).lower()
        raw = (build_result.raw_output or "").lower()

        if any(marker in raw or marker in combined_error for marker in ["timed out", "timeout", "command timed out"]):
            return FailureType.TIMEOUT

        if any(marker in combined_error or marker in raw for marker in ["cs1002", "cs1513", "syntax error", "unexpected token"]):
            return FailureType.SYNTAX

        if any(marker in combined_error or marker in raw for marker in ["cs0246", "cs1503", "cs0103", "cannot find symbol", "type mismatch", "is not defined"]):
            return FailureType.TYPE_SEMANTIC

        if any(marker in raw for marker in ["connection refused", "econnreset", "killed"]):
            return FailureType.ENV_TRANSIENT

        if any(marker in raw or marker in combined_error for marker in ["file not found", "no such file", "permission denied", "executable not found"]):
            return FailureType.PROCESS_FAILURE

        return FailureType.BUILD_FAILED


