from recovery.history import RecoveryHistory
from recovery.classifier import FailureType


def test_format_history_preserves_root_cause_tail():
    history = RecoveryHistory(task_id="TASK_FAIL")

    # Long traceback ending with root-cause AssertionError
    traceback_lines = [
        "Traceback (most recent call last):",
        "  File '/workspace/runner/executor.py', line 145, in execute_suite",
        "    self._run_single_test(test_name)",
        "  File '/workspace/runner/executor.py', line 89, in _run_single_test",
        "    test_func()",
        "  File '/workspace/tests/test_feature.py', line 42, in test_feature_flag",
        "    assert result.status_code == 200, 'Expected status code 200 but got 500 Internal Server Error'",
        "AssertionError: Expected status code 200 but got 500 Internal Server Error",
    ]
    long_traceback = "\n".join(traceback_lines)

    history.record_attempt(
        attempt_index=1,
        model_type_used="FAST",
        failure_type=FailureType.REGRESSION,
        error_message=long_traceback,
    )

    formatted = history.format_history_for_prompt()

    # The prompt must retain the root-cause line from the tail of the error message
    expected_root_cause = "AssertionError: Expected status code 200 but got 500 Internal Server Error"
    assert expected_root_cause in formatted
