"""CHIA nodes for the HINT.GATHER co-design loop.

Each submodule owns one stage of the graph described in ``docs/DESIGN.md``
section 5:

* :mod:`nodes.llvm_nodes`     -- Node 1 (memory profiler) and Node 2 (compiler)
* :mod:`nodes.gem5_nodes`     -- Node 3 (microarchitecture) and Node 6 (O3)
* :mod:`nodes.champsim_nodes` -- Node 4 (fast evaluation)
* :mod:`nodes.evolve`         -- Node 5 (evolutionary search and fitness)
* :mod:`nodes.agents`         -- the LLM implementation and repair agents

Nothing is imported eagerly here: importing :mod:`nodes.gem5_nodes` pulls in
Ray via CHIA, which is unwanted when a caller only needs the pure-Python
genome helpers.
"""

__all__ = [
    "agents",
    "champsim_nodes",
    "evolve",
    "gem5_nodes",
    "llvm_nodes",
]
