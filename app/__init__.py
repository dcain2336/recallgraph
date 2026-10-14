"""RecallGraph agent package.

Modules (implemented during the hackathon event window):
- main: FastAPI app -- /chat, /remember, /graph, /health
- agent: Scout/Synthesizer agent loop over the memory graph
- memory: FalkorDB read/write helpers (tenant-isolated via multigraph)
- cypher_gen: natural-language to read-only Cypher with guardrails
- seed: fictional company-brain seed data
"""
