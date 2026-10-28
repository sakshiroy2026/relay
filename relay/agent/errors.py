"""The tool error the model reads, in a module of its own.

WHAT IT IS  ToolError: an expected tool failure (page not found, site refused,
            not a text page). dispatch_tool turns it into an ok=False result the
            model reads; it is never retried and never counted by the breaker.
            It lives here so relay/agent/real_tools.py can raise it without
            importing relay/agent/tools.py (which imports real_tools).
GOES IN     A message that is safe to show the model (no keys, no internals).
COMES OUT   -
TOUCHES     Nothing.
FAILS WHEN  -
"""


class ToolError(Exception):
    """An expected tool failure (page not found, site down). Shown to the model, not a crash."""
