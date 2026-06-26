from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_managed_2fa_save_uses_payload_signature_to_ignore_stale_form_response():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx").read_text()
    save_block = source[source.index("async function saveManagedPassword"):source.index("\n\n  return (")]

    assert "const latestManaged2FaPayloadSignature = React.useRef('');" in source
    assert "const managed2FaPayload = React.useMemo(() => ({" in source
    assert "password: password.trim()," in source
    assert "reason: reason.trim()," in source
    assert "const managed2FaPayloadSignature = React.useMemo(() => JSON.stringify(managed2FaPayload), [managed2FaPayload]);" in source
    assert "latestManaged2FaPayloadSignature.current = managed2FaPayloadSignature;" in source
    assert "function isActiveManaged2FaRequest(targetAccountId: number, action: Managed2FaAction, requestSeq: number, payloadSignature: string)" in source
    assert "function isCurrentManaged2FaRequest(targetAccountId: number, action: Managed2FaAction, requestSeq: number)" in source

    assert "const payload = managed2FaPayload;" in save_block
    assert "const payloadSignature = managed2FaPayloadSignature;" in save_block
    assert "const trimmedPassword = payload.password;" in save_block
    assert "const trimmedReason = payload.reason;" in save_block
    assert "body: JSON.stringify(payload)," in save_block
    stale_guard = "if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;"
    assert stale_guard in save_block
    assert save_block.index(stale_guard) < save_block.index("setPassword('');")
    assert save_block.index(stale_guard, save_block.index("catch")) < save_block.index("setError(error instanceof Error ? error.message : '保存托管 2FA 失败');")
    assert "if (isCurrentManaged2FaRequest(targetAccountId, action, requestSeq)) setLoading(false);" in save_block
