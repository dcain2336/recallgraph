# RecallGraph -- the agent that never forgets

Graph Hacks (WeMakeDevs x FalkorDB, Oct 15-18, 2026) entry.
An agent whose entire memory is a FalkorDB knowledge graph: episodic + semantic
memory, multi-agent shared state, and every answer shipped with the graph path
that produced it.

**Tracks:** 02 Agent Memory and Coordination (primary); 01 Agents That Act on
Connected Data; 03 Company Brain.

## Tracks

- **Track 02 -- Agent Memory and Coordination (primary target).** Per-tenant
  FalkorDB multigraphs: every conversation turn becomes a timestamped
  `:Episode` node chained with `[:FOLLOWED_BY]`; extracted triples become
  `:Fact` nodes. Scout and Synthesizer roles share the same tenant graph,
  so agents coordinate through graph writes instead of message passing.
- **Track 01 -- Agents That Act on Connected Data.** The Scout runs graph
  algorithms as tools -- shortest-path traversals and degree-centrality
  ranking -- to answer questions like "who should review this change?" from
  multi-hop connections, and every answer ships with the Cypher path that
  produced it.
- **Track 03 -- Company Brain.** Ships with a seeded fictional company graph
  (Arcadia Corp: people, teams, projects, decisions, systems) that judges can
  interrogate: "who owns X?", "why was Y decided?", "what breaks if Z leaves?".

## Quickstart

```bash
cp .env.example .env   # fill FALKORDB_URL + LLM_API_KEY (LLM optional; see below)
docker compose up -d   # local FalkorDB on redis://localhost:6379
python3 -m app.seed     # fictional company brain + demo tenant
uvicorn app.main:app --reload
```

Open http://localhost:8000 -- chat with the agent, watch the reasoning trail,
switch tenants to see memory isolation. Or run the scripted terminal demo
(no browser needed):

```bash
python3 demo/run_demo.py   # 5 demo beats, exits 0 only if all pass
```

No-FalkorDB checks:

```bash
python3 tests/smoke.py
```

## How the agent loop works

1. **Ingest (memory.py).** Every message becomes a timestamped `:Episode` node
   in the tenant's graph (`tenant_<id>`), linked `[:FOLLOWED_BY]` to the
   previous episode. The episode is recorded first, so a later failure can
   never lose the turn.
2. **Retrieve (Scout, agent.py + cypher_gen.py).** An LLM drafts a *read-only*
   Cypher query from the question (schema injected into the system prompt with
   few-shot examples). The query is validated by `validate_cypher` -- it must
   start with MATCH / OPTIONAL MATCH / WITH and is rejected if it contains
   CREATE, MERGE, DELETE, DETACH, SET, REMOVE, DROP, or CALL -- then run
   against the tenant graph AND the shared `company` brain; evidence is merged.
3. **Answer (Synthesizer, agent.py).** A second LLM call composes the answer
   using ONLY the returned evidence rows; the response includes the matched
   path (Cypher + node/edge trail) rendered as the "reasoning trail".
4. **Consolidate (agent.py).** The LLM extracts 0-n new facts from the turn,
   written back as `:Fact` triples with confidence scores via parameterized
   writes -- never raw LLM strings.

The two roles share one graph per tenant (FalkorDB multigraph), so handoffs
are graph writes and either role can resume mid-task.

**Honest note on the keyword fallback:** `cypher_gen.fallback_cypher` maps the
five demo beats to hand-written read-only queries. It is a DEMO FALLBACK ONLY,
used when no LLM key is configured (or the LLM call fails) so the demo still
runs. The primary path is always the LLM drafting Cypher from the schema. The
fallback is documented in the code and disclosed in the demo video -- it is not
presented as the agent's real query engine.

## Layout
- `app/main.py` -- FastAPI: /chat, /remember, /graph, /health, /seed; serves UI
- `app/agent.py` -- Scout/Synthesizer loop
- `app/memory.py` -- graph read/write helpers
- `app/cypher_gen.py` -- NL -> Cypher (read-only guardrails) + demo fallback
- `app/seed.py` -- fictional company seed data (`python3 -m app.seed`)
- `demo/run_demo.py` -- scripted 5-beat terminal demo
- `tests/smoke.py` -- no-FalkorDB checks (validation, guardrails, fallback)
- `ui/index.html` -- chat + trail + mini graph viz (zero dependencies)
