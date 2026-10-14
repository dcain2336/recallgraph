"""Scout/Synthesizer agent loop (implemented during the event).

Two roles, one shared graph (per-tenant FalkorDB multigraph):

- Scout (retrieval specialist): drafts read-only Cypher from the question
  (cypher_gen.generate_cypher), validates it, and runs it against the
  tenant's graph AND the shared company brain, merging the evidence.
  Its tools are graph algorithms: shortest_path (connection explainer)
  and rank_by_degree (e.g. "who should review X?") -- both parameterized,
  both read-only.
- Synthesizer (answer composer): takes Scout's evidence paths and composes
  the final answer using ONLY the evidence, then writes new facts back via
  consolidate (LLM extracts 0-n triples -> add_fact).

Coordination happens through the graph itself: the Scout's evidence is the
set of matched nodes/edges, the Synthesizer's write-back is a graph write,
and either role can resume mid-task from the tenant graph's state.

On ANY LLM failure the loop degrades gracefully: the episode is still
recorded, the evidence path is still returned, and the answer is a
transparently templated summary of the evidence. It never crashes.
"""

import json
from typing import Any, Dict, List, Optional

from . import memory
from .cypher_gen import LLMClient, LLMError, generate_cypher

# ---------------------------------------------------------------------------
# Scout tools (graph algorithms, read-only, parameterized)
# ---------------------------------------------------------------------------

_LABELS = {"Person", "Project", "Decision", "Episode", "Fact", "System"}
_RELS = {"KNOWS", "WORKS_ON", "MADE", "ABOUT", "MENTIONS", "FOLLOWED_BY",
         "SUPPORTS", "DEPENDS_ON", "OWNS"}


def shortest_path(graph: str, name_a: str, name_b: str,
                  max_hops: int = 5) -> List[str]:
    """Shortest node path between two named entities on graph ``graph``.

    ``graph`` is a raw graph name (e.g. memory.COMPANY_GRAPH or
    memory.graph_name(tenant)) -- the caller is responsible for validation.
    Returns a list of display labels (name/text), empty when disconnected.
    """
    hops = max(1, min(int(max_hops), 8))
    rows = memory.run_cypher(
        graph,
        "MATCH p = shortestPath((a)-[*1.." + str(hops) + "]-(b)) "
        "WHERE (a.name = $na OR a.text = $na) AND (b.name = $nb OR b.text = $nb) "
        "RETURN [x IN nodes(p) | coalesce(x.name, x.text, '')] AS path "
        "LIMIT 1",
        {"na": name_a, "nb": name_b},
    )
    if rows and rows[0].get("path"):
        return [str(x) for x in rows[0]["path"] if x]
    return []


def rank_by_degree(graph: str, label: str, rel: str,
                   limit: int = 5) -> List[Dict[str, Any]]:
    """Rank nodes of ``label`` by their count of outgoing ``rel`` edges.

    Label and relationship are allowlisted (no free-form interpolation);
    the classic use is ranking reviewers: Person nodes by WORKS_ON count.
    """
    if label not in _LABELS:
        raise ValueError("label %r not in allowlist" % (label,))
    if rel not in _RELS:
        raise ValueError("relationship %r not in allowlist" % (rel,))
    rows = memory.run_cypher(
        graph,
        "MATCH (n:" + label + ")-[r:" + rel + "]->() "
        "RETURN coalesce(n.name, n.text) AS name, n.role AS role, "
        "count(r) AS degree ORDER BY degree DESC LIMIT $limit",
        {"limit": max(1, min(int(limit), 25))},
    )
    return rows


# ---------------------------------------------------------------------------
# Scout
# ---------------------------------------------------------------------------


def scout(tenant: str, question: str,
          llm: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Retrieve evidence for ``question`` across tenant graph + company brain.

    Returns {cypher, source, records, nodes, edges, error?}. Both graphs are
    queried independently and the row sets are merged; a query that fails on
    one graph does not poison the other.
    """
    llm = llm or LLMClient()
    memory.validate_tenant(tenant)
    gen = generate_cypher(question, llm)
    cypher = gen["cypher"]
    records: List[Dict[str, Any]] = []
    error: Optional[str] = None
    if not gen["valid"]:
        error = "rejected by guardrail: %s" % (gen.get("error"),)
    else:
        try:
            records.extend(memory.query(tenant, cypher))
        except Exception as exc:
            error = "tenant graph query failed: %s" % (exc,)
        try:
            records.extend(memory.query_company(cypher))
        except Exception as exc:
            note = "company graph query failed: %s" % (exc,)
            error = ("%s; %s" % (error, note)) if error else note
    # Trail viz: sample of the tenant graph around the evidence.
    try:
        sample = memory.subgraph(tenant, limit=40)
        nodes, edges = sample["nodes"], sample["edges"]
    except Exception:
        nodes, edges = [], []
    return {
        "cypher": cypher,
        "source": gen.get("source", "llm"),
        "records": records,
        "nodes": nodes,
        "edges": edges,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------

_SYNTH_SYSTEM = (
    "You are the Synthesizer for RecallGraph, an agent whose memory is a "
    "knowledge graph. Answer the user's question using ONLY the evidence rows "
    "below. If the evidence is empty, say plainly that you do not have that "
    "in memory yet. Never invent people, dates, or decisions. Keep the answer "
    "to 2-4 sentences, and cite the entities you used by name."
)


def _render_path_text(records: List[Dict[str, Any]]) -> str:
    """Human-readable one-line-per-record rendering of the evidence path."""
    lines = []
    for row in records[:8]:
        parts = ["%s=%s" % (k, v) for k, v in row.items() if v not in (None, "")]
        lines.append(" ; ".join(parts))
    return "\n".join(lines) if lines else "(no matching nodes/edges)"


def _templated_answer(question: str, records: List[Dict[str, Any]]) -> str:
    """Graceful-degradation answer when the LLM is unavailable.

    Clearly marked as templated so it is never mistaken for an LLM answer.
    """
    if not records:
        return ("I don't have anything in memory about that yet. "
                "(LLM unavailable; this is a templated fallback answer from "
                "the raw graph evidence.)")
    bullets = []
    for row in records[:5]:
        bits = [str(v) for v in row.values() if v not in (None, "")]
        bullets.append("- " + "; ".join(bits))
    return ("Here's what the graph remembers (templated answer, LLM "
            "unavailable):\n" + "\n".join(bullets))


def synthesizer(tenant: str, question: str, evidence: Dict[str, Any],
                llm: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Compose the final answer from Scout's evidence.

    Returns {answer, trail: {cypher, path_text, nodes, edges}, llm_used}.
    """
    llm = llm or LLMClient()
    memory.validate_tenant(tenant)
    records = evidence.get("records", [])
    cypher = evidence.get("cypher", "")
    trail = {
        "cypher": cypher,
        "path_text": _render_path_text(records),
        "nodes": evidence.get("nodes", []),
        "edges": evidence.get("edges", []),
    }
    answer: str
    llm_used = False
    if llm.configured():
        try:
            evidence_json = json.dumps(records[:12], default=str)[:6000]
            answer = llm.complete(
                _SYNTH_SYSTEM,
                "Question: %s\nEvidence rows:\n%s" % (question, evidence_json),
            )
            llm_used = True
        except LLMError:
            answer = _templated_answer(question, records)
    else:
        answer = _templated_answer(question, records)
    return {"answer": answer, "trail": trail, "llm_used": llm_used}


# ---------------------------------------------------------------------------
# Consolidation: extract facts back into the graph
# ---------------------------------------------------------------------------

_CONSOLIDATE_SYSTEM = (
    "Extract durable facts from this conversation turn as a JSON array of "
    "objects with keys subject, predicate, object, confidence (0.0-1.0). "
    "Only include facts worth remembering across sessions (decisions, people "
    "roles, preferences, open questions). Return [] when there is nothing "
    "durable. Return ONLY the JSON array, no other text."
)


def consolidate(tenant: str, question: str, answer: str, episode_id: str,
                llm: Optional[LLMClient] = None) -> List[Dict[str, Any]]:
    """Extract 0-n facts from the turn and write them as :Fact triples.

    Returns the list of facts written. Never raises: LLM failure just means
    no facts are extracted this turn (the episode itself is already stored).
    """
    llm = llm or LLMClient()
    memory.validate_tenant(tenant)
    written: List[Dict[str, Any]] = []
    if not llm.configured():
        return written
    try:
        raw = llm.complete(
            _CONSOLIDATE_SYSTEM,
            "User asked: %s\nAgent answered: %s" % (question, answer),
        )
        facts = json.loads(raw)
        if not isinstance(facts, list):
            return written
        for f in facts:
            if not isinstance(f, dict):
                continue
            s, p, o = f.get("subject"), f.get("predicate"), f.get("object")
            if not (s and p and o):
                continue
            try:
                conf = max(0.0, min(1.0, float(f.get("confidence", 0.7))))
            except (TypeError, ValueError):
                conf = 0.7
            memory.add_fact(tenant, str(s), str(p), str(o), conf)
            written.append({"subject": str(s), "predicate": str(p),
                            "object": str(o), "confidence": conf})
    except Exception:
        # LLM down or returned junk JSON: episode already stored, move on.
        pass
    return written


# ---------------------------------------------------------------------------
# Full loop
# ---------------------------------------------------------------------------


def chat(tenant: str, message: str,
         llm: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Run ingest -> retrieve -> answer -> consolidate for one message.

    Returns {answer, trail, episode_id, facts_written, llm_used}.
    The episode is ALWAYS recorded first, so a later failure can never
    lose the turn. LLM failures degrade to templated answers, never crashes.
    """
    llm = llm or LLMClient()
    memory.validate_tenant(tenant)
    episode_id = memory.add_episode(tenant, message)
    try:
        evidence = scout(tenant, message, llm)
        synth = synthesizer(tenant, message, evidence, llm)
        facts = consolidate(tenant, message, synth["answer"],
                            episode_id, llm)
    except Exception as exc:  # FalkorDB down or unexpected: degrade, never crash
        synth = {
            "answer": ("I couldn't reach my memory graph just now (%s). "
                       "Your message was recorded as an episode and will be "
                       "available when the graph is back." % (exc,)),
            "trail": {"cypher": "", "path_text": "(memory unreachable)",
                      "nodes": [], "edges": []},
            "llm_used": False,
        }
        facts = []
    return {
        "answer": synth["answer"],
        "trail": synth["trail"],
        "episode_id": episode_id,
        "facts_written": facts,
        "llm_used": synth["llm_used"],
    }
