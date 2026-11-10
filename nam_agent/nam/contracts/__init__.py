"""The data contracts the NAM flows read through (plan section 4, Step 10).

base.py says what each contract takes and returns (its input and output
schemas, its side-effect class) and the two failures a backend may raise.
kg.py answers them from the NRL tender knowledge graph. Live SAP and Aarohan
backends are future work behind the same contracts: a backend is anything
with one method per contract name, taking the validated arguments and
returning a result that passes the output schema.

Flows never call a backend directly: they call nam_agent.nam.tools.call(), which
checks, retries, deduplicates and audits every call.
"""
