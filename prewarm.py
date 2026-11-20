"""Start ACP during Hermes gateway startup, in the process that handles turns."""
import logging
import os
import sys
import threading


def start_gateway_prewarm(profile):
    if "gateway" not in sys.argv or os.getenv("HERMES_OPENCODE_ACP_PREWARM", "1") == "0":
        return None
    try:
        args = sys.argv[1:]
        if args[:1] in (["-p"], ["--profile"]):
            args = args[2:]
        if args[:1] and args[0].startswith("--profile="):
            args = args[1:]
        if args[:2] != ["gateway", "run"]:
            return None
        from hermes_constants import get_hermes_home
        import yaml
        with (get_hermes_home() / "config.yaml").open(encoding="utf-8-sig") as handle:
            model = (yaml.safe_load(handle) or {}).get("model")
        if not isinstance(model, dict) or model.get("provider") != profile.name:
            return None
        client = profile.create_client()
    except Exception as exc:
        logging.getLogger(__name__).warning("ACP prewarm unavailable (%s)", type(exc).__name__)
        return None

    def prepare():
        try:
            client.prewarm()
        except Exception as exc:
            # A real request can retry the normal startup path. Avoid logging
            # CLI output, configuration, or operator paths from exceptions.
            logging.getLogger(__name__).warning("ACP prewarm failed (%s)", type(exc).__name__)

    threading.Thread(target=prepare, name="opencode-acp-prewarm", daemon=True).start()
    return client
