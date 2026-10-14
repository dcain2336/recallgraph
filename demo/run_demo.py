"""RecallGraph hackathon demo runner (WeMakeDevs x FalkorDB Graph Hacks).

Runs five labeled beats against a freshly seeded memory graph:

  1. Semantic memory: what did we decide about the deploy freeze?
  2. Graph reasoning: who should review the auth refactor?
  3. Episodic memory: what was my open question from Tuesday?
  4. Tenant isolation: a fresh tenant knows nothing about Tuesday.
  5. Graph-growth evidence: raw node/edge counts per graph.

Usage:  python3 demo/run_demo.py   (from the repo root; works from any cwd)

Requires a live FalkorDB (FALKORDB_URL, default redis://localhost:6379).
No LLM is required: this run uses the documented deterministic keyword
fallback in app/cypher_gen.py (fallback_cypher), which maps the five demo
beats to hand-written read-only queries. That fallback is DEMO-ONLY --
the production path always tries the LLM first via generate_cypher().
"""

import os
import sys

# Make the repo root importable so `python3 demo/run_demo.py` works from
# any cwd (repo root = parent of this file's directory).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app import agent, memory, seed  # noqa: E402
from app.cypher_gen import LLMClient  # noqa: E402


def beat(n, label):
    print("\n" + "=" * 70)
    print("BEAT %d: %s" % (n, label))
    print("=" * 70)


def fail(n, reason):
    print("BEAT %d FAILED: %s" % (n, reason))
    sys.exit(1)


def graph_counts(graph_name):
    nodes = memory.run_cypher(graph_name, "MATCH (n) RETURN count(n) AS c")
    edges = memory.run_cypher(graph_name, "MATCH ()-[r]->() RETURN count(r) AS c")
    return nodes[0]["c"], edges[0]["c"]


def main():
    if not memory.ping():
        print("FALKORDB NOT REACHABLE at %s -- start FalkorDB and retry."
              % os.environ.get("FALKORDB_URL", "redis://localhost:6379"))
        sys.exit(1)

    # Deterministic run: wipe and reseed company brain + demo tenant.
    seed.main()
    # Fresh slate for the isolation beat (beat 4).
    memory.clear_graph(memory.graph_name("other"))

    # LLM-free run: an unconfigured LLMClient means generate_cypher() falls
    # back to the documented keyword fallback in cypher_gen.py.
    llm = LLMClient()
    print("\nLLM configured: %s -- %s"
          % (llm.configured(),
             "using LLM path" if llm.configured()
             else "no LLM key: the documented keyword fallback "
                  "(cypher_gen.fallback_cypher) carries all beats; "
                  "production uses the LLM"))

    names = [p["name"] for p in seed.PEOPLE]

    # -- Beat 1: semantic memory (company brain) ---------------------------
    beat(1, "What did we decide about the deploy freeze last week?")
    r1 = agent.chat("demo",
                    "What did we decide about the deploy freeze last week?",
                    llm=llm)
    print("Cypher: %s" % r1["trail"]["cypher"])
    print("Answer: %s" % r1["answer"])
    if not r1["answer"].strip():
        fail(1, "empty answer")
    if "freeze" not in r1["answer"].lower():
        fail(1, "answer does not mention the freeze")
    print("-> beat 1 OK: freeze decision recalled")

    # -- Beat 2: graph reasoning (who should review?) ----------------------
    beat(2, "Who should review the auth refactor?")
    r2 = agent.chat("demo", "Who should review the auth refactor?", llm=llm)
    print("Cypher: %s" % r2["trail"]["cypher"])
    print("Answer: %s" % r2["answer"])
    if not r2["answer"].strip():
        fail(2, "empty answer")
    if not any(n in r2["answer"] for n in names):
        fail(2, "no seeded person name appears in the answer")
    print("-> beat 2 OK: reviewers ranked from WORKS_ON edges")

    # -- Beat 3: episodic memory (new session, same tenant) -----------------
    beat(3, "New session, same tenant -- my open question from Tuesday")
    r3 = agent.chat("demo", "What was my open question from Tuesday?",
                    llm=llm)
    print("Cypher: %s" % r3["trail"]["cypher"])
    print("Answer: %s" % r3["answer"])
    low3 = r3["answer"].lower()
    if not r3["answer"].strip():
        fail(3, "empty answer")
    if not ("rewrite" in low3 or "refactor" in low3 or "question" in low3):
        fail(3, "Tuesday open question not recalled")
    print("-> beat 3 OK: episodic memory spans sessions")

    # -- Beat 4: per-tenant isolation --------------------------------------
    # Honest note: agent.chat() records the current message as an :Episode
    # FIRST (documented in agent.py), so the just-asked question matches
    # itself in the fresh tenant's graph and the templated answer is the
    # non-empty form. Isolation is therefore proven the way that actually
    # holds: none of tenant "demo"'s seeded content may appear in tenant
    # "other"'s answer, and the company brain holds zero :Episode nodes.
    beat(4, "Switch tenant -- 'other' knows nothing about Tuesday")
    r4 = agent.chat("other", "What was my open question from Tuesday?",
                    llm=llm)
    print("Cypher: %s" % r4["trail"]["cypher"])
    print("Answer: %s" % r4["answer"])
    low4 = r4["answer"].lower()
    if "rewrite" in low4 or "refactor it in place" in low4:
        fail(4, "content from tenant 'demo' leaked into tenant 'other'")
    leak = memory.query(
        "other",
        "MATCH (e:Episode) "
        "WHERE toLower(e.text) CONTAINS 'rewrite the auth service' "
        "RETURN count(e) AS c")
    if leak and leak[0].get("c"):
        fail(4, "tenant 'other' graph contains tenant 'demo' episodes")
    company_eps = memory.query_company(
        "MATCH (e:Episode) RETURN count(e) AS c")
    if company_eps and company_eps[0].get("c"):
        fail(4, "company brain must not hold :Episode nodes")
    print("-> beat 4 OK: no cross-tenant leakage; company brain has "
          "no episodes")

    # -- Beat 5: graph-growth evidence --------------------------------------
    beat(5, "Browser side-by-side: node/edge counts per graph")
    c_nodes, c_edges = graph_counts(memory.COMPANY_GRAPH)
    d_nodes, d_edges = graph_counts(memory.graph_name("demo"))
    print("graph 'company'      : %d nodes, %d edges" % (c_nodes, c_edges))
    print("graph 'tenant_demo'  : %d nodes, %d edges" % (d_nodes, d_edges))
    if c_nodes <= 0 or c_edges <= 0:
        fail(5, "company graph is empty")
    if d_nodes <= 0 or d_edges <= 0:
        fail(5, "tenant_demo graph is empty")
    print("-> beat 5 OK: both graphs non-empty")

    print("\nALL BEATS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
