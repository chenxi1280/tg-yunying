"""Verify the two persisted Gateway result formats without rewriting old journals."""
from app.timezone import as_beijing

from .runtime_state_hash import canonical_state_hash


def journal_proves_gateway_return(row, journal):
    if (journal["journal_state"] != "recorded" or journal["journal_mutation"] != "true"
            or not (journal["journal_message_id"] or journal["journal_fact_id"])):
        return False
    if (row["call_at"] is None or journal["journal_observed_at"] is None
            or as_beijing(journal["journal_observed_at"]) < as_beijing(row["call_at"])):
        return False
    pairs = (("attempt_request", "journal_request"),
        ("attempt_request_hash", "journal_request_hash"),
        ("attempt_target_hash", "journal_target_hash"))
    if any(not row[left] or row[left] != journal[right] for left, right in pairs):
        return False
    return _result_hashes_match(journal)


def _result_hashes_match(journal):
    result = {"remote_message_id": journal["journal_message_id"],
        "remote_fact_id": journal["journal_fact_id"],
        "typed_remote_fact": journal["journal_typed_fact"] or {},
        "failure_code": journal["journal_failure_code"],
        "remote_mutation_state": journal["journal_mutation"]}
    formats = (result,)
    if not result["typed_remote_fact"]:
        # Before typed_remote_fact was added, both hashes covered these four fields.
        formats += ({key: value for key, value in result.items() if key != "typed_remote_fact"},)
    return any(_matches(journal, candidate) for candidate in formats)


def _matches(journal, result):
    proof = {"gateway_request_identity": journal["journal_request"],
        "request_fingerprint": journal["journal_request_hash"], "result": result}
    return (canonical_state_hash(result) == journal["journal_result_hash"]
        and canonical_state_hash(proof) == journal["journal_evidence_hash"])
