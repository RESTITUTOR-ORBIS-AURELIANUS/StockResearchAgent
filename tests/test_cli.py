"""真实 CLI 的异步执行和报告输出边界。"""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime

from stock_research_agent import cli
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import TargetType
from stock_research_agent.graph.nodes.report_composer import report_composer_node


def _incomplete_report():
    state = {
        "run_id": "run_20260826_cli",
        "as_of": datetime.fromisoformat("2026-08-26T15:30:00+08:00"),
        "target": ResearchTarget(
            type=TargetType.MARKET,
            code="A_SHARE",
            name="A股市场",
        ),
        "errors": [],
    }
    return report_composer_node(state)["research_report"]


def test_run_once_uses_ainvoke_and_closes_runtime(monkeypatch, tmp_path) -> None:
    report = _incomplete_report()
    calls: list[dict] = []
    lifecycle: list[str] = []

    class FakeRuntime:
        async def ainvoke(self, state):
            calls.append(state)
            return {"research_report": report}

    @asynccontextmanager
    async def fake_open_runtime(_settings):
        lifecycle.append("open")
        try:
            yield FakeRuntime()
        finally:
            lifecycle.append("closed")

    monkeypatch.setattr(cli.ResearchRuntimeSettings, "from_env", lambda: object())
    monkeypatch.setattr(cli, "open_research_runtime", fake_open_runtime)
    output = tmp_path / "report.json"
    args = argparse.Namespace(
        as_of="2026-08-26T15:30:00+08:00",
        output_format="json",
        output=output,
    )

    exit_code = asyncio.run(cli.run_once(args))

    assert exit_code == 3
    assert lifecycle == ["open", "closed"]
    assert len(calls) == 1
    assert calls[0]["target"] == ResearchTarget(
        type=TargetType.MARKET,
        code="A_SHARE",
        name="A股市场",
    )
    assert calls[0]["as_of"].utcoffset() is not None
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["report_id"] == report.report_id


def test_main_rejects_naive_as_of_before_loading_runtime(capsys) -> None:
    exit_code = cli.main(
        [
            "--as-of",
            "2026-08-26T15:30:00",
        ]
    )

    assert exit_code == 1
    assert "必须包含时区" in capsys.readouterr().err


def test_parse_args_defaults_to_markdown_stdout() -> None:
    args = cli.parse_args(
        [
            "--as-of",
            "2026-08-26T15:30:00+08:00",
        ]
    )

    assert args.output_format == "markdown"
    assert args.output is None
