from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_usage_reports_metrics_refresh_ignores_stale_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/UsageReportsView.tsx").read_text()
    load_block = source[source.index("async function loadMetrics"):source.index("\n\n  React.useEffect")]

    assert "const metricsRequestSeq = React.useRef(0);" in source
    assert "function beginMetricsRequest()" in source
    assert "function isActiveMetricsRequest(requestSeq: number)" in source
    assert "const requestSeq = beginMetricsRequest();" in load_block
    assert "const nextMetrics = await api<OperationMetricsSummary>('/operation-metrics/summary');" in load_block
    assert "if (!isActiveMetricsRequest(requestSeq)) return;" in load_block
    assert "setMetrics(nextMetrics);" in load_block

    catch_block = load_block[load_block.index("catch (error)"):]
    assert catch_block.index("if (!isActiveMetricsRequest(requestSeq)) return;") < catch_block.index("setMetricsError")
    assert "if (isActiveMetricsRequest(requestSeq)) setLoadingMetrics(false);" in load_block
