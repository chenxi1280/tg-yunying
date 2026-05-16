from scripts.run_capacity_benchmark import parse_scenario, simulate


def test_capacity_model_reports_zero_duplicate_sends():
    result = simulate(
        parse_scenario("1000:30"),
        gateway_mode="unknown_after_send",
        duration_minutes=10,
        db_pool_size=20,
        db_max_overflow=20,
        global_tg_rate_per_second=30,
        account_rate_per_hour=120,
    )

    assert result.recommended_dispatcher_workers == 8
    assert result.duplicate_sends == 0
    assert result.unknown_after_send > 0
    assert result.recommended_dispatcher_concurrency <= 38


def test_capacity_model_identifies_backlog_when_account_capacity_is_tight():
    result = simulate(
        parse_scenario("100:5"),
        gateway_mode="fast",
        duration_minutes=10,
        db_pool_size=20,
        db_max_overflow=20,
        global_tg_rate_per_second=30,
        account_rate_per_hour=1,
    )

    assert result.backlog_actions > 0
    assert result.oldest_pending_p95_seconds > 0
    assert result.bottleneck == "account_capacity"
