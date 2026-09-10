"""Keep the original send observation across normal and recovery settlement."""


def record_send_diagnostics(action, diagnostics, *, attempt=None) -> None:
    if not diagnostics:
        return
    action.result = {**dict(action.result or {}), "send_diagnostics": dict(diagnostics)}
    if attempt is not None:
        attempt.result_snapshot = {
            **dict(attempt.result_snapshot or {}), "send_diagnostics": dict(diagnostics),
        }
