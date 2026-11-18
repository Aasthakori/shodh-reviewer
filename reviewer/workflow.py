"""Workflow graph: structural checks and route evaluation.

A workflow artifact is a directed graph of versioned nodes:

    decision: {"type": "decision", "rev": 2, "condition": <cond>,
               "on_true": "<node id>", "on_false": "<node id>",
               "implements": ["R1"]}
    route:    {"type": "route", "rev": 1, "route": "manual_review"}

Evaluation walks from `start`. A missing or dangling edge yields NO_ROUTE at
that node (reported as a missing branch); a cycle yields NO_ROUTE as well.
"""
from __future__ import annotations

from dataclasses import dataclass

from .conditions import ConditionError, evaluate, normalize

NO_ROUTE = "NO_ROUTE"


@dataclass
class WorkflowResult:
    route: str
    path: list[str]            # node ids visited
    decided_by: str | None     # last decision node on the path
    refs: list[str]
    problem: str | None = None


class Workflow:
    def __init__(self, data: dict, fields: dict):
        self.data = data
        self.start = data.get("start")
        self.nodes: dict = data.get("nodes", {})
        self.errors: list[tuple[str, str]] = []      # (node id, message): unsupported content
        self.structure: list[tuple[str, str]] = []   # (node id, message): structural defects
        self.conds: dict[str, dict] = {}
        if self.start not in self.nodes:
            self.structure.append(("start", f"start node {self.start!r} does not exist"))
        for nid, node in self.nodes.items():
            t = node.get("type")
            if t == "decision":
                try:
                    self.conds[nid] = normalize(node["condition"], fields)
                except (ConditionError, KeyError) as exc:
                    self.errors.append((nid, f"condition not in supported language: {exc}"))
                for edge in ("on_true", "on_false"):
                    tgt = node.get(edge)
                    if tgt is None:
                        self.structure.append((nid, f"decision has no {edge} branch"))
                    elif tgt not in self.nodes:
                        self.structure.append((nid, f"{edge} points to missing node {tgt!r}"))
            elif t == "route":
                if not node.get("route"):
                    self.structure.append((nid, "route node has no destination"))
            else:
                self.errors.append((nid, f"unsupported node type {t!r}"))
        # graph reachability ignoring conditions
        seen, stack = set(), [self.start] if self.start in self.nodes else []
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            node = self.nodes[n]
            for edge in ("on_true", "on_false"):
                if node.get(edge) in self.nodes:
                    stack.append(node[edge])
        self.graph_reachable = seen
        for nid in self.nodes:
            if nid not in seen:
                self.structure.append((nid, "node is not connected to the start node"))

    @property
    def usable(self) -> bool:
        return not self.errors and self.start in self.nodes

    def run(self, ticket: dict) -> WorkflowResult:
        path: list[str] = []
        nid = self.start
        decided_by = None
        took_true = False
        while True:
            if nid is None or nid not in self.nodes:
                last = path[-1] if path else "start"
                return WorkflowResult(NO_ROUTE, path, decided_by, self._refs(decided_by, took_true),
                                      f"branch from {last} has no valid destination")
            if nid in path:
                return WorkflowResult(NO_ROUTE, path, decided_by, self._refs(decided_by, took_true), f"cycle at {nid}")
            path.append(nid)
            node = self.nodes[nid]
            if node["type"] == "route":
                return WorkflowResult(node["route"], path, decided_by, self._refs(decided_by, took_true))
            decided_by = nid
            took_true = evaluate(self.conds[nid], ticket)
            nid = node.get("on_true") if took_true else node.get("on_false")

    def _refs(self, nid: str | None, took_true: bool) -> list[str]:
        """A decision's `implements` refs describe its TRUE branch; a ticket that
        leaves the last decision on its FALSE branch falls through to the default."""
        if nid is None or not took_true:
            return []
        return list(self.nodes[nid].get("implements", []))
