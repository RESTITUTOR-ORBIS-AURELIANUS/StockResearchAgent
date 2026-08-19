"""把状态、节点和边连接为可执行 LangGraph。"""

from langgraph.graph import END, START, StateGraph

from stock_research_agent.graph.nodes.initialize_run import initialize_run_node
from stock_research_agent.graph.state import ResearchGraphState


def build_research_graph():
    """构建当前已经实现的正式工作流切片。"""

    builder = StateGraph(ResearchGraphState)
    builder.add_node("initialize_run", initialize_run_node)
    builder.add_edge(START, "initialize_run")
    builder.add_edge("initialize_run", END)
    return builder.compile()
