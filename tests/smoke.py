"""No-FalkorDB smoke tests for RecallGraph.

Runs with:  python3 tests/smoke.py   (from the repo root)

Covers everything that can be checked without a live graph:
- tenant id validation (accept/reject)
- Cypher guardrail: accepts good read queries, rejects write/injection
  attempts, and accepts queries that merely mention write words inside
  string literals
- the documented keyword fallback maps all five demo beats to queries
- module imports (memory, cypher_gen, agent, seed, main) work offline
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   - %s" % name)
    else:
        FAIL += 1
        print("FAIL - %s" % name)


# ---------------------------------------------------------------- imports
try:
    from app import memory, cypher_gen, agent, seed, main  # noqa: F401
    check("imports (memory, cypher_gen, agent, seed, main)", True)
except Exception as exc:
    check("imports (memory, cypher_gen, agent, seed, main): %s" % exc, False)
    print("imports failed; aborting")
    sys.exit(1)

# ---------------------------------------------------------------- tenant validation
for good in ["demo", "abc", "a-b_c", "x" * 32, "0"]:
    try:
        memory.validate_tenant(good)
        check("tenant accepts %r" % good, True)
    except ValueError:
        check("tenant accepts %r" % good, False)

for bad in ["", "UPPER", "has space", "x" * 33, "semi;colon", "../etc",
            "quo'te", None, 123]:
    try:
        memory.validate_tenant(bad)
        check("tenant rejects %r" % (bad,), False)
    except (ValueError, TypeError):
        check("tenant rejects %r" % (bad,), True)

check("graph_name('demo') == 'tenant_demo'",
      memory.graph_name("demo") == "tenant_demo")

# ---------------------------------------------------------------- cypher guardrail
good_queries = [
    "MATCH (d:Decision) WHERE toLower(d.text) CONTAINS 'freeze' RETURN d LIMIT 25",
    "MATCH (p:Person)-[r:WORKS_ON]->(pr:Project) RETURN p.name LIMIT 5",
    "OPTIONAL MATCH (e:Episode) RETURN e LIMIT 10",
    "WITH 1 AS x MATCH (n) WHERE n.x = x RETURN n LIMIT 1",
    # write-looking words inside string literals must NOT trip the guardrail
    "MATCH (n) WHERE n.text = 'please CREATE this and DELETE that' RETURN n LIMIT 5",
    'MATCH (n) WHERE n.text = "MERGE is fine in quotes" RETURN n LIMIT 5',
]
for q in good_queries:
    valid, err = cypher_gen.validate_cypher(q)
    check("guardrail accepts: %.60s" % q, valid)

bad_queries = [
    "CREATE (n:Person {name: 'x'})",
    "MATCH (n) DETACH DELETE n",
    "MATCH (n) SET n.x = 1 RETURN n",
    "MATCH (n) REMOVE n.x RETURN n",
    "MATCH (n) MERGE (m:X) RETURN n",
    "MATCH (n) CALL db.labels() YIELD label RETURN label",
    "DROP INDEX foo",
    "MATCH (n); MATCH (m) RETURN n, m",
    "PROFILE MATCH (n) RETURN n",
    "RETURN 1",
    "",
    None,
]
for q in bad_queries:
    valid, err = cypher_gen.validate_cypher(q)
    check("guardrail rejects: %.60s" % (q,), not valid)

# ---------------------------------------------------------------- demo-beat fallback
beats = [
    "What did we decide about the deploy freeze last week?",
    "Who should review the auth refactor?",
    "What was my open question from Tuesday?",
    "Switch tenant: is my memory empty here?",
    "Show me the raw graph node and edge counts",
]
for b in beats:
    q = cypher_gen.fallback_cypher(b)
    valid, _ = cypher_gen.validate_cypher(q or "")
    check("fallback maps beat %.50s -> valid cypher" % b, bool(q) and valid)

check("fallback returns None for empty input",
      cypher_gen.fallback_cypher("") is None)

gen = cypher_gen.generate_cypher("What did we decide about the deploy freeze?")
check("generate_cypher without LLM -> valid fallback",
      gen["valid"] and gen["source"] == "fallback" and bool(gen.get("error")))

# ---------------------------------------------------------------- scout tools allowlists (offline check: bad label/rel raise)
try:
    agent.rank_by_degree("x", "Nope", "WORKS_ON")
    check("rank_by_degree rejects unknown label", False)
except ValueError:
    check("rank_by_degree rejects unknown label", True)
try:
    agent.rank_by_degree("x", "Person", "HACKS")
    check("rank_by_degree rejects unknown rel", False)
except ValueError:
    check("rank_by_degree rejects unknown rel", True)

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
