from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_listener_center_summary_writes_ignore_stale_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/ListenerCenterView.tsx").read_text()
    guards = (PROJECT_ROOT / "frontend/src/app/views/listenerCenterRequestGuards.ts").read_text()
    load_block = source[source.index("async function load("):source.index("\n\n  React.useEffect")]
    switch_block = source[source.index("async function switchListener"):source.index("\n  async function loadListenerEvents")]
    reset_block = source[source.index("async function confirmResetWatermark"):source.index("\n\n  const table")]

    assert "useListenerSummaryRequestGuards()" in source
    assert "const summaryRequestSeq = React.useRef(0);" in guards
    assert "const summaryActionSeq = React.useRef(0);" in guards
    assert "function beginSummaryRequest()" in guards
    assert "function currentSummaryActionSeq()" in guards
    assert "function isActiveSummaryRequest(requestSeq: number, actionSeq: number)" in guards
    assert "function beginSummaryAction()" in guards
    assert "function isActiveSummaryAction(actionSeq: number)" in guards
    assert "const loadRequestSeq = React.useRef(0);" in guards
    assert "function beginLoadRequest()" in guards
    assert "function isActiveLoadRequest(requestSeq: number)" in guards

    assert "const requestSeq = requestGuards.beginSummaryRequest();" in load_block
    assert "const actionSeq = requestGuards.currentSummaryActionSeq();" in load_block
    assert "if (!requestGuards.isActiveSummaryRequest(requestSeq, actionSeq)) return;" in load_block
    assert "const summaryActionSeq = requestGuards.beginSummaryAction();" in switch_block
    assert "if (!requestGuards.isActiveSummaryAction(summaryActionSeq)) return;" in switch_block
    assert "const summaryActionSeq = requestGuards.beginSummaryAction();" in reset_block
    assert "if (!requestGuards.isActiveSummaryAction(summaryActionSeq) ||" in reset_block

    for block in [load_block, switch_block, reset_block]:
        catch_block = block[block.index("catch (err)"):]
        assert "return;" in catch_block
        assert catch_block.index("return;") < catch_block.index("setError")

    assert "const nextSummary = await api<ListenerSummary>('/listeners/summary');" in load_block
    assert "setSummary(nextSummary);" in load_block
    assert "if (requestGuards.isActiveLoadRequest(activeLoadRequestSeq)) setLoading(false);" in load_block
    assert "const nextSummary = await api<ListenerSummary>(`/listeners/${row.object_type}/${rawId}/switch`" in switch_block
    assert "setSummary(nextSummary);" in switch_block
    assert "if (requestGuards.isActiveActionRequest(actionRequestSeq)) setSwitchingKey('');" in switch_block
    assert "const nextSummary = await api<ListenerSummary>(`/listeners/${target.object_type}/${rawId}/reset-watermark`" in reset_block
    assert "setSummary(nextSummary);" in reset_block
    assert "if (requestGuards.isActiveActionRequest(actionRequestSeq)) setSwitchingKey('');" in reset_block


def test_listener_center_detail_writes_ignore_stale_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/ListenerCenterView.tsx").read_text()
    guards = (PROJECT_ROOT / "frontend/src/app/views/listenerCenterRequestGuards.ts").read_text()
    events_block = source[source.index("async function loadListenerEvents"):source.index("\n  async function loadListenerErrors")]
    errors_block = source[source.index("async function loadListenerErrors"):source.index("\n  function openResetWatermark")]

    assert "useListenerDetailRequestGuards()" in source
    assert "function beginDetailRequest()" in guards
    assert "function isActiveDetailRequest(requestSeq: number)" in guards

    for block in [events_block, errors_block]:
        assert "const detailRequestSeq = detailRequestGuards.beginDetailRequest();" in block
        assert "if (!detailRequestGuards.isActiveDetailRequest(detailRequestSeq)) return;" in block
        catch_block = block[block.index("catch (err)"):]
        assert catch_block.index("return;") < catch_block.index("setError")
        assert "if (detailRequestGuards.isActiveDetailRequest(detailRequestSeq)) setDetailLoadingKey('');" in block

    assert "setEventRows((current) => ({ ...current, [row.key]: rows }));" in events_block
    assert "setErrorRows((current) => ({ ...current, [row.key]: rows }));" in errors_block


def test_listener_center_reset_watermark_modal_ignores_stale_sessions():
    source = (PROJECT_ROOT / "frontend/src/app/views/ListenerCenterView.tsx").read_text()
    open_block = source[source.index("function openResetWatermark"):source.index("\n\n  async function confirmResetWatermark")]
    close_block = source[source.index("function closeResetWatermark"):source.index("\n\n  async function confirmResetWatermark")]
    reset_block = source[source.index("async function confirmResetWatermark"):source.index("\n\n  const table")]
    modal_block = source[source.index("<Modal"):source.index("</Modal>", source.index("<Modal"))]

    assert "const resetWatermarkSessionRef = React.useRef({ key: '', seq: 0 });" in source
    assert "resetWatermarkSessionRef.current = { key: row.key, seq: resetWatermarkSessionRef.current.seq + 1 };" in open_block
    assert open_block.index("resetWatermarkSessionRef.current = { key: row.key, seq: resetWatermarkSessionRef.current.seq + 1 };") < open_block.index("setResetTarget(row);")
    assert "resetWatermarkSessionRef.current = { key: '', seq: resetWatermarkSessionRef.current.seq + 1 };" in close_block
    assert "const target = resetTarget;" in reset_block
    assert "const resetSession = resetWatermarkSessionRef.current;" in reset_block
    assert "const [, rawId] = target.key.split(':');" in reset_block
    assert "`/listeners/${target.object_type}/${rawId}/reset-watermark`" in reset_block
    assert "if (!requestGuards.isActiveSummaryAction(summaryActionSeq) || resetWatermarkSessionRef.current.key !== target.key || resetWatermarkSessionRef.current.seq !== resetSession.seq) return;" in reset_block
    assert reset_block.index("resetWatermarkSessionRef.current.key !== target.key || resetWatermarkSessionRef.current.seq !== resetSession.seq") < reset_block.index("setSummary(nextSummary);")
    catch_block = reset_block[reset_block.index("catch (err)"):]
    assert "resetWatermarkSessionRef.current.key !== target.key || resetWatermarkSessionRef.current.seq !== resetSession.seq" in catch_block
    assert catch_block.index("return;") < catch_block.index("setError")
    assert "closeResetWatermark();" in reset_block
    assert "onCancel={closeResetWatermark}" in modal_block
