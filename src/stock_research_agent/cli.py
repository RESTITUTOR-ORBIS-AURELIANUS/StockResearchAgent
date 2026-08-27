"""从命令行执行一次完整研究，并输出确定性最终报告。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import TargetType
from stock_research_agent.reporting import (
    ReportHealth,
    ReportOutcome,
    ResearchReport,
    render_research_report_markdown,
)
from stock_research_agent.runtime import (
    ResearchRuntimeSettings,
    open_research_runtime,
)

OutputFormat = Literal["markdown", "json"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行一次完整的 A 股市场每日投研工作流")
    parser.add_argument(
        "--as-of",
        required=True,
        help="数据截止时间，必须包含时区，例如 2026-08-18T15:30:00+08:00",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        dest="output_format",
        help="报告输出格式，默认 markdown",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选输出文件；未指定时写到标准输出",
    )
    return parser.parse_args(argv)


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--as-of 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of 必须包含时区，例如 +08:00")
    return parsed


def _render_report(report: ResearchReport, output_format: OutputFormat) -> str:
    if output_format == "json":
        return json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    return render_research_report_markdown(report)


def report_exit_code(report: ResearchReport) -> int:
    """0 表示报告完整，3 表示已产出但研究链不完整。"""

    if report.outcome is ReportOutcome.INCOMPLETE:
        return 3
    if report.health is ReportHealth.WITH_ERRORS:
        return 3
    return 0


async def run_once(args: argparse.Namespace) -> int:
    as_of = _parse_as_of(args.as_of)
    target = ResearchTarget(
        type=TargetType.MARKET,
        code="A_SHARE",
        name="A股市场",
    )
    settings = ResearchRuntimeSettings.from_env()

    async with open_research_runtime(settings) as runtime:
        result = await runtime.ainvoke({"target": target, "as_of": as_of})
        report = result.get("research_report")
        if not isinstance(report, ResearchReport):
            raise RuntimeError("研究图结束时没有生成合法的 ResearchReport")
        rendered = _render_report(report, args.output_format)
        if args.output is None:
            print(rendered)
        else:
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(str(args.output.resolve()))
        return report_exit_code(report)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run_once(args))
    except KeyboardInterrupt:
        print("研究运行已由用户中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"研究运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
