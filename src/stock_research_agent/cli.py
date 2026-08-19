"""本地启动研究工作流的命令行入口。"""

import argparse
import json
from datetime import datetime

from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import TargetType
from stock_research_agent.graph import build_research_graph


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动一次 A 股投研工作流")
    parser.add_argument("--code", required=True, help="证券代码，例如 000001.SZ")
    parser.add_argument("--name", required=True, help="证券名称，例如 平安银行")
    parser.add_argument(
        "--as-of",
        required=True,
        help="数据截止时间，必须包含时区，例如 2026-08-18T15:30:00+08:00",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target = ResearchTarget(type=TargetType.STOCK, code=args.code, name=args.name)
    as_of = datetime.fromisoformat(args.as_of)

    graph = build_research_graph()
    result = graph.invoke({"target": target, "as_of": as_of})

    output = {
        "run_id": result["run_id"],
        "target": result["target"].model_dump(mode="json"),
        "as_of": result["as_of"].isoformat(),
        "token_budget_remaining": result["token_budget_remaining"],
        "time_budget_remaining_seconds": result["time_budget_remaining_seconds"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
