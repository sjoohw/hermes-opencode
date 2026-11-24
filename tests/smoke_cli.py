"""Opt-in real OpenCode ACP handshake/config smoke test, without inference.

Unlike the offline suite, this requires OpenCode and may make its normal catalog
network calls and local records. It sends no chat prompt or Hermes user context.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from support import client as acp, constants


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--command", help="Operator-selected OpenCode executable")
    source.add_argument("--npm-prefix", type=Path, help="Test an npm install's actual platform-specific .bin launcher")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--list-models", action="store_true", help="Include advertised model IDs")
    args = parser.parse_args()
    command = args.command
    if args.npm_prefix:
        name = "opencode.cmd" if os.name == "nt" else "opencode"
        command = str((args.npm_prefix / "node_modules/.bin" / name).resolve())
    client = acp.OpenCodeACPClient(command=command)
    try:
        worker = client._ensure_worker(args.timeout)
        with worker.rpc_lock:
            session = worker.new_session(args.timeout)
            model = acp.session_model(session)
        report = {"ok": True, "platform": sys.platform, "bridge_version": constants.VERSION,
                          "command": client.command, "cwd": client.workspace, "session_model": model,
                          "inference_requested": False}
        if args.list_models:
            models = [value for item in acp._config_candidates(session, "model") for value in acp.options(item)]
            legacy = (session.get("models") or {}).get("availableModels") or []
            report["models"] = list(dict.fromkeys(models + [m["modelId"] for m in legacy if m.get("modelId")]))
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
