"""Fictional company-brain seed data (implemented during the event).

Loads the 'Arcadia Corp' demo graph: people, teams, projects, decisions,
systems and their relationships -- the Company Brain judges can interrogate
("who owns X?", "why was Y decided?", "what breaks if Z leaves?").
Fictional data only; no real persons.

The seed is idempotent: main() clears the ``company`` brain graph and the
``demo`` tenant graph first, then rebuilds everything from the module-level
lists below. The demo runner (demo/run_demo.py) reads PEOPLE/PROJECTS/
DECISIONS/SYSTEMS to assert answers against the seeded content.

All Cypher writes use parameters ($name style); values are never
interpolated into query strings. No :Episode nodes are written into the
company graph -- episodic memory lives only in tenant graphs, which is what
demo beat 4 (per-tenant isolation) relies on.
"""

from . import memory

# ---------------------------------------------------------------------------
# Seed data (module-level so the demo script can reference the names).
# All names, teams, and systems are fictional.
# ---------------------------------------------------------------------------

PEOPLE = [
    {"name": "Ada Kowalczyk", "role": "Tech Lead", "team": "platform"},
    {"name": "Bruno Ferro", "role": "Backend Engineer", "team": "platform"},
    {"name": "Cleo Marchetti", "role": "DevOps Engineer", "team": "platform"},
    {"name": "Dario Nilsen", "role": "Data Engineer", "team": "data"},
    {"name": "Esme Okafor", "role": "ML Engineer", "team": "data"},
    {"name": "Felix Brandt", "role": "iOS Engineer", "team": "mobile"},
    {"name": "Greta Lundqvist", "role": "Android Engineer", "team": "mobile"},
    {"name": "Hassan Rahimi", "role": "QA Engineer", "team": "mobile"},
]

PROJECTS = [
    {"name": "auth-refactor", "status": "active", "owner": "Ada Kowalczyk"},
    {"name": "payments-v2", "status": "active", "owner": "Bruno Ferro"},
    {"name": "ml-pipeline", "status": "beta", "owner": "Esme Okafor"},
    {"name": "mobile-app", "status": "planning", "owner": "Felix Brandt"},
]

FREEZE_TEXT = (
    "Deploy freeze last week: no production deploys until the "
    "auth-refactor security review is complete"
)

DECISIONS = [
    {"text": FREEZE_TEXT, "date": "2026-10-07", "decider": "Ada Kowalczyk"},
    {"text": "Adopt trunk-based development across all repos",
     "date": "2026-09-21", "decider": "Bruno Ferro"},
    {"text": "Migrate ml-pipeline feature store to streaming ingestion",
     "date": "2026-10-02", "decider": "Esme Okafor"},
    {"text": "Sunset legacy password auth endpoints by 2027-01-31",
     "date": "2026-09-30", "decider": "Ada Kowalczyk"},
    {"text": "Ship mobile-app beta to internal testers only",
     "date": "2026-10-10", "decider": "Felix Brandt"},
]

SYSTEMS = [
    {"name": "auth-service"},
    {"name": "payments-db"},
    {"name": "ml-serve"},
    {"name": "edge-cdn"},
]

WORKS_ON = [
    ("Ada Kowalczyk", "auth-refactor"),
    ("Bruno Ferro", "auth-refactor"),
    ("Cleo Marchetti", "auth-refactor"),
    ("Bruno Ferro", "payments-v2"),
    ("Dario Nilsen", "ml-pipeline"),
    ("Esme Okafor", "ml-pipeline"),
    ("Felix Brandt", "mobile-app"),
    ("Greta Lundqvist", "mobile-app"),
    ("Hassan Rahimi", "mobile-app"),
]

KNOWS = [
    ("Ada Kowalczyk", "Bruno Ferro"),
    ("Bruno Ferro", "Cleo Marchetti"),
    ("Dario Nilsen", "Esme Okafor"),
    ("Felix Brandt", "Greta Lundqvist"),
    ("Ada Kowalczyk", "Esme Okafor"),
]

DEPENDS_ON = [
    ("auth-refactor", "auth-service"),
    ("payments-v2", "payments-db"),
    ("ml-pipeline", "ml-serve"),
    ("mobile-app", "edge-cdn"),
]

OWNS = [
    ("Ada Kowalczyk", "auth-service"),
    ("Bruno Ferro", "payments-db"),
    ("Esme Okafor", "ml-serve"),
    ("Felix Brandt", "edge-cdn"),
]

# Two semantic facts that support the deploy-freeze decision.
FREEZE_FACTS = [
    {"subject": "auth-refactor",
     "predicate": "security_review_status",
     "object": "incomplete"},
    {"subject": "auth-refactor security review",
     "predicate": "blocks",
     "object": "production deploys"},
]

# Demo tenant episodic + semantic seed (the "Tuesday open question" is beat 3).
DEMO_EPISODES = [
    "Open question from Tuesday: should we rewrite the auth service or "
    "refactor it in place? Still undecided.",
    "Discussed the deploy freeze with the platform team; releases on hold "
    "until the review lands.",
    "Esme demoed the new ml-pipeline dashboard; latency down 18 percent "
    "since the beta cutover.",
]

DEMO_FACT = ("deploy pipeline", "frozen_until", "2026-10-19", 0.9)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _w(cypher, params):
    """Write one parameterized query against the company brain graph."""
    memory.get_graph(memory.COMPANY_GRAPH).query(cypher, params)


def _seed_company():
    for p in PEOPLE:
        _w("MERGE (n:Person {name: $name}) "
           "SET n.role = $role, n.team = $team",
           {"name": p["name"], "role": p["role"], "team": p["team"]})
    for pr in PROJECTS:
        _w("MERGE (n:Project {name: $name}) "
           "SET n.status = $status, n.owner = $owner",
           {"name": pr["name"], "status": pr["status"], "owner": pr["owner"]})
    for d in DECISIONS:
        _w("MERGE (n:Decision {text: $text}) "
           "SET n.date = $date, n.decider = $decider",
           {"text": d["text"], "date": d["date"], "decider": d["decider"]})
    for s in SYSTEMS:
        _w("MERGE (n:System {name: $name})", {"name": s["name"]})

    for person, project in WORKS_ON:
        _w("MATCH (p:Person {name: $p}), (pr:Project {name: $pr}) "
           "MERGE (p)-[:WORKS_ON]->(pr)",
           {"p": person, "pr": project})
    for a, b in KNOWS:
        _w("MATCH (x:Person {name: $a}), (y:Person {name: $b}) "
           "MERGE (x)-[:KNOWS]->(y)",
           {"a": a, "b": b})
    # The freeze decision was made by a platform person (Ada) and is about
    # the auth-refactor project.
    _w("MATCH (p:Person {name: $p}), (d:Decision {text: $t}) "
       "MERGE (p)-[:MADE]->(d)",
       {"p": "Ada Kowalczyk", "t": FREEZE_TEXT})
    _w("MATCH (d:Decision {text: $t}), (pr:Project {name: $pr}) "
       "MERGE (d)-[:ABOUT]->(pr)",
       {"t": FREEZE_TEXT, "pr": "auth-refactor"})
    for project, system in DEPENDS_ON:
        _w("MATCH (pr:Project {name: $pr}), (s:System {name: $s}) "
           "MERGE (pr)-[:DEPENDS_ON]->(s)",
           {"pr": project, "s": system})
    for person, system in OWNS:
        _w("MATCH (p:Person {name: $p}), (s:System {name: $s}) "
           "MERGE (p)-[:OWNS]->(s)",
           {"p": person, "s": system})
    for f in FREEZE_FACTS:
        _w("MERGE (f:Fact {subject: $s, predicate: $p, object: $o})",
           {"s": f["subject"], "p": f["predicate"], "o": f["object"]})
        _w("MATCH (f:Fact {subject: $s, predicate: $p, object: $o}), "
           "(d:Decision {text: $t}) MERGE (f)-[:SUPPORTS]->(d)",
           {"s": f["subject"], "p": f["predicate"], "o": f["object"],
            "t": FREEZE_TEXT})
    # NOTE: no :Episode nodes in the company graph, by design.


def _seed_demo_tenant():
    for text in DEMO_EPISODES:
        memory.add_episode("demo", text)
    subject, predicate, obj, conf = DEMO_FACT
    memory.add_fact("demo", subject, predicate, obj, conf)


def _counts(graph_name):
    nodes = memory.run_cypher(
        graph_name, "MATCH (n) RETURN count(n) AS c")
    edges = memory.run_cypher(
        graph_name, "MATCH ()-[r]->() RETURN count(r) AS c")
    return nodes[0]["c"], edges[0]["c"]


def main():
    """Clear and reseed the company brain and the demo tenant (idempotent)."""
    memory.clear_graph(memory.COMPANY_GRAPH)
    memory.clear_graph(memory.graph_name("demo"))
    _seed_company()
    _seed_demo_tenant()

    c_nodes, c_edges = _counts(memory.COMPANY_GRAPH)
    d_nodes, d_edges = _counts(memory.graph_name("demo"))
    print("Seeded company brain graph '%s': %d nodes, %d edges"
          % (memory.COMPANY_GRAPH, c_nodes, c_edges))
    print("Seeded tenant graph '%s': %d nodes, %d edges"
          % (memory.graph_name("demo"), d_nodes, d_edges))
    print("  people=%d projects=%d decisions=%d systems=%d"
          % (len(PEOPLE), len(PROJECTS), len(DECISIONS), len(SYSTEMS)))


if __name__ == "__main__":
    main()
