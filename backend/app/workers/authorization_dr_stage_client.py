from __future__ import annotations


def post_bundle_stages(client, operation_id: str, owner: dict, receipt: dict) -> None:
    common = {
        "bundle_generation": receipt["bundle_generation"],
        "ciphertext_digest": receipt["ciphertext_digest"],
        "inventory_sequence": receipt["inventory_sequence"],
    }
    for stage in ("local_copy_verified", "snapshot_copy_verified", "inventory_persisted"):
        post_stage(client, operation_id, owner, stage, receipt["ciphertext_digest"], **common)


def post_stage(client, operation_id: str, owner: dict, stage: str, digest: str, **evidence) -> None:
    client.post(
        f"/internal/v1/authorization-dr/operations/{operation_id}/stage-facts",
        {**owner, "stage": stage, "manifest_digest": digest, **evidence},
    )


__all__ = ["post_bundle_stages", "post_stage"]
