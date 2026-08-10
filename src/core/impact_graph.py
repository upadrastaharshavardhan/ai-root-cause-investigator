"""Build and rank an impact graph correlating commits, files, tests, and developers."""

from __future__ import annotations

from collections import defaultdict

import networkx as nx

from src.models.schemas import (
    CommitInfo,
    EvidenceBundle,
    ImpactEdge,
    ImpactGraph,
    ImpactNode,
    InvestigationContext,
    TestFailureInfo,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ImpactGraphBuilder:
    """
    Constructs a directed weighted graph:
      developer → commit → file → test → failure
    and ranks nodes by centrality + failure proximity.
    """

    def build(
        self,
        context: InvestigationContext,
        evidence: list[EvidenceBundle],
    ) -> ImpactGraph:
        G = nx.DiGraph()
        nodes: dict[str, ImpactNode] = {}
        edges: list[ImpactEdge] = []

        def add_node(nid: str, label: str, ntype: str, weight: float = 1.0, **meta: object) -> None:
            if nid not in nodes:
                nodes[nid] = ImpactNode(
                    id=nid, label=label, node_type=ntype, weight=weight, metadata=dict(meta)
                )
                G.add_node(nid, label=label, type=ntype, weight=weight)

        def add_edge(src: str, tgt: str, relation: str, weight: float = 1.0) -> None:
            edges.append(ImpactEdge(source=src, target=tgt, relation=relation, weight=weight))
            G.add_edge(src, tgt, relation=relation, weight=weight)

        # Developers & commits & files
        for commit in context.recent_commits:
            c_id = f"commit:{commit.short_sha}"
            d_id = f"dev:{commit.author}"
            add_node(c_id, commit.short_sha, "commit", weight=1.0, message=commit.message)
            add_node(d_id, commit.author, "developer", weight=1.0)
            add_edge(d_id, c_id, "authored")

            for f in commit.files:
                f_id = f"file:{f.path}"
                add_node(f_id, f.path, "file", weight=1.0 + (f.additions + f.deletions) / 50.0)
                add_edge(c_id, f_id, "modified", weight=1.0 + (f.additions + f.deletions) / 100.0)

                # Module heuristic: top-level dir or package
                parts = f.path.replace("\\", "/").split("/")
                if len(parts) > 1:
                    mod = parts[0] if parts[0] not in ("src", "tests", "test") else (
                        parts[1] if len(parts) > 1 else parts[0]
                    )
                    m_id = f"module:{mod}"
                    add_node(m_id, mod, "module", weight=1.0)
                    add_edge(f_id, m_id, "belongs_to")

        # Tests & failures
        for test in context.failed_tests:
            t_id = f"test:{test.test_title}"
            add_node(t_id, test.test_title, "test", weight=2.0, error=test.error_message[:200])
            fail_id = f"failure:{test.test_id or test.test_title}"
            add_node(fail_id, "FAILURE", "failure", weight=5.0)
            add_edge(t_id, fail_id, "failed_on", weight=3.0)

            # Heuristic link: if test path or title mentions a changed file
            test_path = (test.file_path or "").lower()
            title_l = test.test_title.lower()
            for commit in context.recent_commits:
                for f in commit.files:
                    fname = f.path.lower()
                    base = fname.split("/")[-1].split(".")[0]
                    if base and (base in title_l or base in test_path or fname in test_path):
                        f_id = f"file:{f.path}"
                        add_edge(f_id, t_id, "covers", weight=2.0)

        # Evidence nodes (lightweight)
        for bundle in evidence:
            for item in bundle.items[:30]:
                e_id = f"evidence:{item.id}"
                add_node(e_id, item.title[:60], "evidence", weight=0.5, etype=item.type.value)
                for ent in item.related_entities[:5]:
                    # Try to link to existing nodes
                    for prefix in ("file:", "test:", "commit:", "module:"):
                        candidate = f"{prefix}{ent}"
                        if candidate in nodes:
                            add_edge(e_id, candidate, "supports", weight=0.8)

        # Rank by PageRank + failure proximity
        ranked: list[str] = []
        if G.number_of_nodes() > 0:
            try:
                pr = nx.pagerank(G, weight="weight", alpha=0.85)
                # Boost nodes that can reach a failure
                failure_nodes = [n for n, d in G.nodes(data=True) if d.get("type") == "failure"]
                boost: dict[str, float] = defaultdict(float)
                for fn in failure_nodes:
                    for pred in nx.ancestors(G, fn):
                        boost[pred] += 1.0
                    boost[fn] += 2.0
                scored = {
                    n: pr.get(n, 0.0) * 0.6 + boost.get(n, 0.0) * 0.4 for n in G.nodes()
                }
                ranked = sorted(scored, key=scored.get, reverse=True)[:30]
            except Exception as exc:  # noqa: BLE001
                logger.warning("pagerank_failed", error=str(exc))
                ranked = list(G.nodes())[:30]

        return ImpactGraph(
            nodes=list(nodes.values()),
            edges=edges,
            ranked_root_candidates=ranked,
        )
