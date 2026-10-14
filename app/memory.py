"""FalkorDB memory helpers (implemented during the event).

RecallGraph's entire memory is a FalkorDB knowledge graph:

- Tenant isolation: one named FalkorDB graph per tenant, selected with
  GRAPH.QUERY against ``tenant_<id>``. A shared company brain lives in
  graph ``company``.
- Episodic memory: every conversation turn is an ``:Episode`` node
  ``{id, text, ts, tenant}`` chained with ``[:FOLLOWED_BY]``.
- Semantic memory: ``:Fact`` nodes ``{subject, predicate, object,
  confidence, ts, tenant}`` extracted during consolidation.

Conventions:
- ``add_episode(tenant, text)`` returns the episode id (uuid4) and links
  ``FOLLOWED_BY`` from the previously latest episode by ts.
- ``add_fact(tenant, subject, predicate, object, confidence)`` writes one triple.
- ``query(tenant, cypher, params)`` / ``query_company(cypher, params)`` are
  the read path and return rows as ``list[dict]`` keyed by the query header.
- ``subgraph(tenant, limit)`` returns ``{nodes: [{id, labels, props}],
  edges: [{src, dst, type}]}`` for the UI graph visualization.
- ALL writes go through Cypher parameters -- values are never interpolated
  into query strings. Tenant ids are validated against
  ``^[a-z0-9_-]{1,32}$`` before touching a graph name.

Reads return plain dicts/lists so FastAPI can serialize them directly.
"""

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from falkordb import FalkorDB

TENANT_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
COMPANY_GRAPH = "company"

_client: Optional[FalkorDB] = None


def validate_tenant(tenant: str) -> str:
    """Validate a tenant id; returns it unchanged or raises ValueError."""
    if not isinstance(tenant, str) or not TENANT_RE.match(tenant):
        raise ValueError(
            f"Invalid tenant id {tenant!r}: must match ^[a-z0-9_-]{{1,32}}$"
        )
    return tenant


def graph_name(tenant: str) -> str:
    """Return the FalkorDB graph name for a validated tenant id."""
    return f"tenant_{validate_tenant(tenant)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_client() -> FalkorDB:
    """Singleton FalkorDB client built from ``FALKORDB_URL``.

    ``FALKORDB_URL`` defaults to ``redis://localhost:6379``. No connection
    is opened until the first query runs.
    """
    global _client
    if _client is None:
        url = os.environ.get("FALKORDB_URL", "redis://localhost:6379")
        _client = FalkorDB(url=url)
    return _client


def get_graph(name: str):
    """Return the named Graph object (no validation applied -- internal use)."""
    return get_client().select_graph(name)


def tenant_graph(tenant: str):
    """Return the Graph object for a validated tenant."""
    return get_graph(graph_name(tenant))


def ping() -> bool:
    """True if FalkorDB answers, False on any connection error."""
    try:
        # falkordb-py's FalkorDB has no .ping(); a trivial write-query
        # both proves liveness and works on a fresh server.
        get_graph("__ping").query("RETURN 1")
        return True
    except Exception:
        return False


def _rows_to_dicts(result) -> List[Dict[str, Any]]:
    """Convert a FalkorDB QueryResult into a list of JSON-safe dicts."""
    header = getattr(result, "header", []) or []
    names: List[str] = []
    for h in header:
        if isinstance(h, (tuple, list)):
            names.append(str(h[-1]))
        else:
            names.append(str(h))
    out: List[Dict[str, Any]] = []
    for row in getattr(result, "result_set", []) or []:
        entry: Dict[str, Any] = {}
        for i, name in enumerate(names):
            value = row[i] if i < len(row) else None
            entry[name or f"col_{i}"] = _json_safe(value)
        out.append(entry)
    return out


def _json_safe(value: Any) -> Any:
    """Best-effort conversion of a graph value to JSON-serializable data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    node_id = getattr(value, "id", None)
    labels = getattr(value, "labels", None)
    props = getattr(value, "properties", None)
    if labels is not None and props is not None:  # Node-like
        return {"id": node_id, "labels": list(labels), "props": _json_safe(props)}
    relation = getattr(value, "relation", None)
    if relation is not None:  # Edge-like
        return {
            "type": relation,
            "src": getattr(value, "src_node", None),
            "dst": getattr(value, "dest_node", None),
        }
    return str(value)


def run_cypher(name: str, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Run a read-only Cypher query against a named graph; returns row dicts."""
    graph = get_graph(name)
    result = graph.ro_query(cypher, params or {})
    return _rows_to_dicts(result)


def query(tenant: str, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Read path against a tenant's graph."""
    return run_cypher(graph_name(tenant), cypher, params)


def query_company(cypher: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Read path against the shared ``company`` brain graph."""
    return run_cypher(COMPANY_GRAPH, cypher, params)


def clear_graph(name: str) -> None:
    """Delete a whole named graph (used by seed.py for idempotency)."""
    try:
        get_graph(name).delete()
    except Exception:
        # Deleting a nonexistent graph raises; that is fine for seeding.
        pass


def add_episode(tenant: str, text: str) -> str:
    """Append an :Episode node for a tenant; chain FOLLOWED_BY from the
    previous latest episode. Returns the new episode id."""
    validate_tenant(tenant)
    name = graph_name(tenant)
    graph = get_graph(name)
    episode_id = uuid.uuid4().hex
    ts = _now_iso()

    prev: List[Dict[str, Any]] = []
    try:
        prev = run_cypher(
            name,
            "MATCH (e:Episode) RETURN e.id AS id ORDER BY e.ts DESC LIMIT 1",
        )
    except Exception:
        # Fresh tenant: graph key does not exist yet for read-only queries.
        prev = []
    prev_id = prev[0]["id"] if prev else None

    graph.query(
        "CREATE (n:Episode {id: $id, text: $text, ts: $ts, tenant: $tenant})",
        {"id": episode_id, "text": text, "ts": ts, "tenant": tenant},
    )
    if prev_id:
        graph.query(
            "MATCH (p:Episode {id: $prev}), (n:Episode {id: $cur}) "
            "CREATE (p)-[:FOLLOWED_BY]->(n)",
            {"prev": prev_id, "cur": episode_id},
        )
    return episode_id


def add_fact(
    tenant: str,
    subject: str,
    predicate: str,
    object_: str,
    confidence: float = 1.0,
) -> None:
    """Write one semantic :Fact triple for a tenant. All parameterized."""
    validate_tenant(tenant)
    get_graph(graph_name(tenant)).query(
        "CREATE (:Fact {subject: $s, predicate: $p, object: $o, "
        "confidence: $c, ts: $ts, tenant: $tenant})",
        {
            "s": str(subject),
            "p": str(predicate),
            "o": str(object_),
            "c": float(confidence),
            "ts": _now_iso(),
            "tenant": tenant,
        },
    )


def subgraph(tenant: str, limit: int = 60) -> Dict[str, List[Dict[str, Any]]]:
    """Sample ``{nodes, edges}`` from a tenant's graph for the UI viz.

    Nodes: ``{id, labels, props}``. Edges: ``{src, dst, type}``.
    Deduplicated; deterministic ordering by node id.
    """
    validate_tenant(tenant)
    rows = run_cypher(
        graph_name(tenant),
        "MATCH (n) OPTIONAL MATCH (n)-[r]-(m) "
        "RETURN id(n) AS nid, labels(n) AS nlabels, properties(n) AS nprops, "
        "id(m) AS mid, labels(m) AS mlabels, properties(m) AS mprops, "
        "type(r) AS rtype "
        "LIMIT $limit",
        {"limit": int(limit)},
    )
    nodes: Dict[Any, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    seen_edges = set()
    for row in rows:
        nid = row.get("nid")
        if nid is not None and nid not in nodes:
            nodes[nid] = {
                "id": nid,
                "labels": row.get("nlabels") or [],
                "props": row.get("nprops") or {},
            }
        mid = row.get("mid")
        rtype = row.get("rtype")
        if mid is not None and rtype:
            if mid not in nodes:
                nodes[mid] = {
                    "id": mid,
                    "labels": row.get("mlabels") or [],
                    "props": row.get("mprops") or {},
                }
            key = (nid, mid, rtype)
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"src": nid, "dst": mid, "type": rtype})
    ordered = sorted(nodes.values(), key=lambda n: str(n["id"]))
    return {"nodes": ordered, "edges": edges}
