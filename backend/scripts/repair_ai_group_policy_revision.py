"""Preview/apply/readback the exact missing current content-policy bindings."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select, text

from app.database import SessionLocal
from app.models import Task, TaskAiContentPolicyBinding
from app.services.task_center.ai_content_binding_revision import (
    apply_content_binding_revision, preview_content_binding_revision,
)
from app.services.task_center.production_e4_scope import configure_readonly_snapshot
from app.services.task_center.runtime_state_hash import canonical_state_hash


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--mode', choices=('preview', 'apply', 'readback'), required=True)
    result.add_argument('--input', required=True)
    result.add_argument('--output', required=True)
    result.add_argument('--expected-deployed-sha', required=True)
    result.add_argument('--actor', default='')
    result.add_argument('--approval-reference', default='')
    return result


def _require_runtime(expected):
    if len(expected) != 40 or os.getenv('RELEASE_SHA') != expected:
        raise RuntimeError('content_revision_deployed_sha_mismatch')


def _write(path, value):
    with open(path, 'x', encoding='utf-8', opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        json.dump(value, out, ensure_ascii=False, indent=2, default=str)
        out.write('\n')


def preview(session, spec, expected_sha):
    ids = spec['task_ids']
    if not ids or len(ids) != len(set(ids)) or len(ids) != spec['expected_count']:
        raise ValueError('content_revision_exact_scope_invalid')
    rows = []
    for task_id in sorted(ids):
        task = session.get(Task, task_id)
        if task is None or task.tenant_id != spec['tenant_id'] or task.status != 'running':
            raise ValueError('content_revision_task_scope_invalid')
        rows.append(preview_content_binding_revision(session, task_id,
            source_revision=task.config_revision - 1,
            restore_original_references=spec.get("restore_original_references", False)))
    body = {'deployed_sha': expected_sha, 'tenant_id': spec['tenant_id'],
            'expected_count': len(rows), 'items': rows}
    return {**body, 'fingerprint': canonical_state_hash(body)}


def apply(session, document, options):
    body = {key: value for key, value in document.items() if key != 'fingerprint'}
    if (document['deployed_sha'] != options.expected_deployed_sha
            or canonical_state_hash(body) != document['fingerprint']
            or len(document['items']) != document['expected_count']):
        raise RuntimeError('content_revision_manifest_invalid')
    results = []
    for item in document['items']:
        if item['tenant_id'] != document['tenant_id']:
            raise RuntimeError('content_revision_tenant_mismatch')
        results.append(apply_content_binding_revision(session, item,
            actor=options.actor, approval_reference=options.approval_reference))
    return {'deployed_sha': options.expected_deployed_sha, 'expected_count': len(results),
            'fingerprint': document['fingerprint'], 'items': results}


def readback(session, receipt, expected_sha):
    if receipt['deployed_sha'] != expected_sha or len(receipt['items']) != receipt['expected_count']:
        raise RuntimeError('content_revision_receipt_invalid')
    for item in receipt['items']:
        binding = session.get(TaskAiContentPolicyBinding, item['binding_id'])
        task = session.get(Task, item['task_id'])
        if (binding is None or task is None or binding.task_id != task.id
                or binding.task_config_revision != item['revision']
                or binding.task_lifecycle_epoch != task.task_lifecycle_epoch
                or task.config_revision != item['revision']
                or binding.evidence_hash != item['evidence_hash']
                or list(binding.attestation_ids) != list(task.type_config.get('ai_content_attestation_ids') or [])):
            raise RuntimeError('content_revision_readback_mismatch')
    return {**receipt, 'status': 'persisted_verified', 'business_status': 'unproven'}


def main():
    options = parser().parse_args()
    _require_runtime(options.expected_deployed_sha)
    document = json.loads(Path(options.input).read_text())
    if Path(options.output).exists():
        raise FileExistsError('content_revision_output_already_exists')
    with SessionLocal() as session:
        if options.mode != 'apply':
            configure_readonly_snapshot(session)
        else:
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.execute(text("SET LOCAL statement_timeout = '20s'"))
        if options.mode == 'preview':
            result = preview(session, document, options.expected_deployed_sha)
        elif options.mode == 'apply':
            result = apply(session, document, options)
            session.commit()
        else:
            result = readback(session, document, options.expected_deployed_sha)
    _write(options.output, result)
    print(json.dumps({'mode': options.mode, 'count': result['expected_count'],
                      'status': result.get('status', options.mode)}))


if __name__ == '__main__':
    main()
