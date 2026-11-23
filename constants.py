"""Non-secret release settings; no discovery, network, or filesystem writes."""
import os

from .launcher import resolve_opencode

VERSION = "0.6.1"
DEFAULT_ARGS = ("acp", "--pure", "--port", "4885")
PROVIDER = "hermes-opencode-acp"
DEFAULT_MODEL = "opencode-default"  # Hermes label for the CLI-selected model.
ARGS_ENV = "HERMES_OPENCODE_ACP_ARGS"
AGENT_ENV = "HERMES_OPENCODE_ACP_AGENT"
RELAY_AGENT = "hermes-relay"  # Tool-less agent from opencode/hermes-relay.jsonc.
# Startup retries: no listener port, then CLIs (OpenCode 2.x) without --pure.
FALLBACK_ARGS = (("acp", "--pure"), ("acp",))


def opencode_command():
    """Resolve an operator-owned executable, never a model-supplied command."""
    return resolve_opencode()


def opencode_args():
    """Operator-owned CLI arguments, e.g. "acp" for builds without --pure."""
    override = os.environ.get(ARGS_ENV, "").split()
    return override or list(DEFAULT_ARGS)


def opencode_agent():
    """Operator-selected OpenCode agent (ACP "mode"), or None for the session default."""
    return os.environ.get(AGENT_ENV, "").strip() or None
