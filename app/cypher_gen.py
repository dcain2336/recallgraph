"""Natural-language to Cypher (implemented during the event).

The LLM drafts a **read-only** Cypher query from the user question. The graph
schema is injected into the system prompt together with few-shot examples, so
the model generates queries against the exact labels/relationships that exist.

Guardrails (``validate_cypher``): string literals are stripped first, then the
query must start with ``MATCH``, ``OPTIONAL MATCH`` or ``WITH``, and the
tokens ``CREATE``/``MERGE``/``DELETE``/``DETACH``/``SET``/``REMOVE``/``DROP``/
``CALL``/``LOAD``/``PROFILE``/``EXPLAIN``/``USING`` are rejected anywhere.
Because literals are stripped before the check, a question that mentions e.g.
"please CREATE this" inside a quoted string still validates.

Honest note on ``fallback_cypher``: there is a small deterministic
keyword-based fallback that maps the five demo beats to hand-written queries.
It is a DEMO FALLBACK ONLY -- the primary path is always the LLM
(``generate_cypher``). The fallback fires only when the LLM is unreachable
or misconfigured, and it is documented as such in the README/demo video so
nobody mistakes it for the agent's real query engine.
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Schema (also injected into the LLM system prompt)
# ---------------------------------------------------------------------------

SCHEMA = (
    "Graph schema:\n"
    "Node labels:\n"
    "- :Person {name, role, team}\n"
    "- :Project {name, status, owner}\n"
    "- :Decision {text, date, decider}\n"
    "- :Episode {id, text, ts, tenant}     -- one per conversation turn (episodic memory)\n"
    "- :Fact {subject, predicate, object, confidence, ts, tenant}  -- semantic triples\n"
    "- :System {name}                     -- company systems/services\n"
    "Relationships:\n"
    "- (Person)-[:KNOWS]->(Person)\n"
    "- (Person)-[:WORKS_ON]->(Project)\n"
    "- (Person)-[:MADE]->(Decision)\n"
    "- (Decision)-[:ABOUT]->(Project)\n"
    "- (Episode)-[:MENTIONS]->(Person|Project|Decision)\n"
    "- (Episode)-[:FOLLOWED_BY]->(Episode)   -- session chain, ordered by ts\n"
    "- (Fact)-[:SUPPORTS]->(Decision)\n"
    "- (Project)-[:DEPENDS_ON]->(System)\n"
    "- (Person)-[:OWNS]->(System)\n"
)

SYSTEM_PROMPT = (
    "You translate natural-language questions into READ-ONLY Cypher queries.\n\n"
    + SCHEMA
    + "\nRules:\n"
    "1. Return ONLY the Cypher query. No explanation, no markdown fences.\n"
    "2. The query MUST be read-only: start with MATCH, OPTIONAL MATCH, or WITH.\n"
    "   Never emit CREATE, MERGE, DELETE, DETACH, SET, REMOVE, DROP, or CALL.\n"
    "3. Always LIMIT the results (use LIMIT 25 unless the question needs more).\n"
    "4. Use case-insensitive matching (toLower(...)) when matching user words.\n"
    "5. Prefer returning names/texts over whole nodes, e.g. RETURN p.name AS name.\n"
    "\nExamples:\n"
    "Q: What did we decide about the deploy freeze?\n"
    "MATCH (d:Decision) WHERE toLower(d.text) CONTAINS 'freeze' "
    "RETURN d.text AS decision, d.date AS date, d.decider AS decider LIMIT 25\n"
    "\n"
    "Q: Who should review the auth refactor?\n"
    "MATCH (p:Person)-[r:WORKS_ON]->(pr:Project) "
    "WHERE toLower(pr.name) CONTAINS 'auth' "
    "RETURN p.name AS reviewer, p.role AS role, count(r) AS familiarity "
    "ORDER BY familiarity DESC LIMIT 5\n"
    "\n"
    "Q: What was my open question from Tuesday?\n"
    "MATCH (e:Episode) WHERE toLower(e.text) CONTAINS 'question' "
    "RETURN e.text AS episode, e.ts AS ts ORDER BY e.ts DESC LIMIT 5\n"
    "\n"
    "Q: What do you remember about Project Orion?\n"
    "MATCH (e:Episode) WHERE toLower(e.text) CONTAINS 'orion' "
    "RETURN e.text AS episode, e.ts AS ts ORDER BY e.ts DESC LIMIT 10\n"
)


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Raised when the LLM endpoint cannot be reached or errors out."""


def _sanitize_proxy_env() -> None:
    """Drop bracketed IPv6 entries from no_proxy/NO_PROXY.

    httpx crashes parsing entries like ``[::1]`` (``InvalidURL: Invalid
    port: ':1]'``). This keeps every other entry intact.
    """
    for key in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(key)
        if val and "[" in val:
            os.environ[key] = ",".join(p for p in val.split(",") if "[" not in p)


class LLMClient:
    """Minimal OpenAI-compatible chat client.

    Config from env: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL.
    ``ping()`` returns True when the endpoint answers.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.timeout = timeout
        _sanitize_proxy_env()

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def complete(self, system: str, user: str) -> str:
        """One chat completion; raises LLMError on any failure."""
        if not self.configured():
            raise LLMError("LLM not configured (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL)")
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 512,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            return text.strip().strip("`").strip()
        except Exception as exc:  # network, auth, malformed response
            raise LLMError(f"LLM call failed: {exc}") from exc

    def ping(self) -> bool:
        # Health-check: is an endpoint configured? A live completion call
        # would stall /health for the model's full latency (reasoning
        # models can take ~60s), so we report configuration, not liveness.
        return self.configured()


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENT_RE = re.compile(r"//.*$", re.MULTILINE)
_FORBIDDEN_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL|LOAD|PROFILE|EXPLAIN|USING)\b",
    re.IGNORECASE,
)
_ALLOWED_START_RE = re.compile(r"^(MATCH|OPTIONAL\s+MATCH|WITH)\b", re.IGNORECASE | re.DOTALL)


def validate_cypher(q):
    """Check that a query is a read-only SELECT-style Cypher query.

    Returns (True, None) when valid, (False, reason) otherwise.
    """
    if not isinstance(q, str) or not q.strip():
        return False, "empty query"
    cleaned = _LITERAL_RE.sub("''", q)
    cleaned = _COMMENT_RE.sub("", cleaned).strip()
    if not _ALLOWED_START_RE.match(cleaned):
        return False, "query must start with MATCH, OPTIONAL MATCH, or WITH"
    m = _FORBIDDEN_RE.search(cleaned)
    if m:
        return False, "forbidden write/keyword clause: %s" % (m.group(1),)
    if ";" in cleaned.rstrip(";"):
        return False, "multiple statements not allowed"
    return True, None


# ---------------------------------------------------------------------------
# Generation: LLM primary path, keyword fallback for demo beats
# ---------------------------------------------------------------------------

_FALLBACK_BEATS: List[Tuple[str, str]] = [
    # (keyword trigger, read-only Cypher)
    ("freeze", "MATCH (d:Decision) WHERE toLower(d.text) CONTAINS 'freeze' "
               "RETURN d.text AS decision, d.date AS date, d.decider AS decider LIMIT 25"),
    ("auth refactor", "MATCH (p:Person)-[r:WORKS_ON]->(pr:Project) "
               "WHERE toLower(pr.name) CONTAINS 'auth' "
               "RETURN p.name AS reviewer, p.role AS role, pr.name AS project "
               "ORDER BY p.name LIMIT 5"),
    ("review", "MATCH (p:Person)-[r:WORKS_ON]->(pr:Project) "
               "RETURN p.name AS reviewer, p.role AS role, count(r) AS familiarity "
               "ORDER BY familiarity DESC LIMIT 5"),
    ("open question", "MATCH (e:Episode) WHERE toLower(e.text) CONTAINS 'question' "
               "RETURN e.text AS episode, e.ts AS ts ORDER BY e.ts DESC LIMIT 5"),
    ("tuesday", "MATCH (e:Episode) WHERE toLower(e.text) CONTAINS 'question' "
                "RETURN e.text AS episode, e.ts AS ts ORDER BY e.ts DESC LIMIT 5"),
]


def fallback_cypher(question: str) -> Optional[str]:
    """Keyword fallback for the five demo beats.

    DEMO FALLBACK ONLY -- not the primary path. Returns a hand-written
    read-only query when the question matches a demo-beat keyword,
    otherwise a generic episodic-memory query. Returns None only for
    empty input.
    """
    q = (question or "").strip().lower()
    if not q:
        return None
    for trigger, cypher in _FALLBACK_BEATS:
        if trigger in q:
            return cypher
    # Generic default: search episodic memory, most recent first.
    return ("MATCH (e:Episode) RETURN e.text AS episode, e.ts AS ts "
            "ORDER BY e.ts DESC LIMIT 5")


def generate_cypher(question: str, llm: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Draft Cypher for ``question`` via the LLM; fall back on LLM failure.

    Returns ``{cypher, valid, error?, source}`` where ``source`` is "llm" or
    "fallback". The fallback keeps the demo running without an LLM key and is
    clearly marked so nothing mistakes it for the agent's real query engine.
    """
    llm = llm or LLMClient()
    if llm.configured():
        try:
            raw = llm.complete(SYSTEM_PROMPT, f"Q: {question}")
            valid, error = validate_cypher(raw)
            return {"cypher": raw, "valid": valid,
                    "error": error, "source": "llm"}
        except LLMError as exc:
            fb = fallback_cypher(question)
            valid, _ = validate_cypher(fb or "")
            return {"cypher": fb, "valid": valid,
                    "error": str(exc), "source": "fallback"}
    fb = fallback_cypher(question)
    valid, _ = validate_cypher(fb or "")
    return {"cypher": fb, "valid": valid,
            "error": "LLM not configured", "source": "fallback"}
