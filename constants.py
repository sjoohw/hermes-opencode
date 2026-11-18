"""Non-secret release settings; no discovery, network, or filesystem writes."""
from .launcher import resolve_opencode

VERSION = "0.4.0"
PROVIDER = "hermes-opencode-acp"
DEFAULT_MODEL = "opencode-default"  # Hermes label for the CLI-selected model.


def opencode_command():
    """Resolve an operator-owned executable, never a model-supplied command."""
    return resolve_opencode()
