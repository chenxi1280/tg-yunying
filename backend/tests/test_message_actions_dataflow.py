from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"\n  async function {function_name}")
    candidates = [
        source.find("\n  async function", start + 1),
        source.find("\n  function", start + 1),
        source.find("\n\n  return", start + 1),
    ]
    end = min(candidate for candidate in candidates if candidate != -1)
    return source[start:end]


def test_message_actions_distinguish_refresh_failure_from_write_failure():
    source = (PROJECT_ROOT / "frontend/src/app/context/messageActions.ts").read_text()

    assert "async function refreshMessageDataAfterAction(actionLabel: string" in source
    assert "params.showResult('消息发送数据刷新失败'" in source
    assert "操作已完成，但刷新消息发送数据失败" in source

    helper_start = source.index("async function refreshMessageDataAfterAction")
    helper_end = source.index("\n\n  function startDirectMessageToContact", helper_start)
    helper_body = source[helper_start:helper_end]
    assert "await params.refresh();" in helper_body
    assert "params.handleActionError(" not in helper_body

    for function_name in [
        "createDirectMessageTask",
        "createMessageSendTask",
        "cancelTask",
        "dispatchTask",
        "drainQueue",
        "retryTask",
    ]:
        body = _function_body(source, function_name)
        assert "await refreshMessageDataAfterAction(" in body
        refresh_block = body[body.index("await refreshMessageDataAfterAction(") :]
        if "} catch" in refresh_block:
            assert "params.handleActionError(" not in refresh_block[: refresh_block.index("} catch")]
        else:
            assert "params.handleActionError(" not in refresh_block
