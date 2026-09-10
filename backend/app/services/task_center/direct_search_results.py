"""Require direct transport proof without erasing already observed remote effects."""
from app.search_transport import direct_proof_matches, is_direct_search


def normalize_direct_search_result(runtime: dict, result: dict) -> dict:
    if not is_direct_search(runtime):
        return result
    observed = {**result, "transport_contract": dict(runtime)}
    if result.get("remote_mutation_started") is False and not (
        result.get("success") or result.get("target_click_observed") or result.get("clicked")
        or result.get("click_outcomes") or result.get("execution_status") in {"confirmed", "unknown_after_click"}
    ):
        return observed
    if direct_proof_matches(runtime, result.get("transport_evidence")):
        return observed
    return {
        **observed, "success": False, "gateway_outcome_unknown": True,
        "error_code": "direct_search_transport_evidence_missing",
        "detail": "已返回搜索结果但直连传输证据不完整，保留远端事实并等待核对",
    }
