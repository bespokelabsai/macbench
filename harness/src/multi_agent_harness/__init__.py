"""Multi-agent harness: the board and proxy (run on the hub) and the Harbor plugin (runs on the host).

`multi_agent_harness:TeamEnvironment` and `multi_agent_harness:TeamAgent` are short names for
`multi_agent_harness.harbor.environment:TeamEnvironment` and `multi_agent_harness.harbor.agent:TeamAgent`.
They load on first use, so importing the package on the hub (no harbor installed) stays light.
"""

_LAZY = {"TeamEnvironment": "multi_agent_harness.harbor.environment",
         "TeamAgent": "multi_agent_harness.harbor.agent"}

__all__ = list(_LAZY)


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
