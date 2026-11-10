"""nam-agent — a minimal, transparent, local-first NAM harness.

Four pillars, one module each:
  harness  → nam_agent/runtime + nam_agent/gateway  (scaffolding around the raw LLM)
  loop     → nam_agent/loop                      (observe → reason → act → repeat)
             nam_agent/graph                     (opt-in structure around the loop — extends this pillar)
  memory   → nam_agent/memory                    (procedural / semantic / episodic)
  ops      → nam_agent/ops + evals/              (trace → eval → gate → release)
"""

__version__ = "0.1.8"
