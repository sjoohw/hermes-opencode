"""Hermes OpenCode ACP, using only Hermes's documented provider interface."""
from providers import register_provider
from providers.base import ProviderProfile

from .prewarm import start_gateway_prewarm
from .constants import ARGS_ENV, DEFAULT_MODEL, VERSION, opencode_command


class OpenCodeACPProfile(ProviderProfile):
    def create_client(self, **client_kwargs):
        from hermes_constants import get_hermes_home
        from .acp_client import SharedACPClient

        if not client_kwargs.get("command") and not client_kwargs.get("acp_command"):
            client_kwargs["command"] = opencode_command()
        return SharedACPClient(get_hermes_home(), **client_kwargs)

    def fetch_models(self, *, api_key=None, base_url=None, timeout=15.0):
        return [DEFAULT_MODEL]


profile = OpenCodeACPProfile(
    name="hermes-opencode-acp",
    display_name="Hermes OpenCode ACP",
    description=f"Connect Hermes to your installed OpenCode CLI via ACP (bridge {VERSION})",
    auth_type="external_process",
    api_mode="chat_completions",
    base_url="acp://hermes-opencode-acp",
    process_command=opencode_command(),
    process_args=(),
    process_command_env_vars=("HERMES_OPENCODE_ACP_COMMAND",),
    process_args_env_var=ARGS_ENV,
    supports_health_check=False,
    fallback_models=(DEFAULT_MODEL,),
)
register_provider(profile)


# Provider discovery also runs for install and diagnostic commands. Only a
# serving gateway should hold a warm client across conversations.
_warm_client = start_gateway_prewarm(profile)
