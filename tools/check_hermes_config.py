"""Check that a Hermes profile routes chat through this bridge and nowhere else.

Read-only. Prints config key paths and provider names, never other values, so
API keys and endpoints in the profile are not echoed. Exit status 1 on errors.

  python tools/check_hermes_config.py --forbid codellm.example.com --forbid codellm
"""
import argparse
import os
from pathlib import Path
import sys

PROVIDER = "hermes-opencode-acp"
FALLBACK_KEYS = ("fallback_model", "fallback_providers")


def _entries(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _strings(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def check(config, forbid=()):
    """Return (errors, warnings, notes) for a parsed Hermes config mapping."""
    errors, warnings, notes = [], [], []
    config = config if isinstance(config, dict) else {}
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    if model.get("provider") != PROVIDER:
        errors.append(f"model.provider is {model.get('provider')!r}, not {PROVIDER!r}: main chat bypasses OpenCode")

    delegation = config.get("delegation") if isinstance(config.get("delegation"), dict) else {}
    if delegation.get("provider") not in (None, "", PROVIDER):
        warnings.append(f"delegation.provider is {delegation['provider']!r}: subagents bypass OpenCode")
    chains = [(key, config.get(key)) for key in FALLBACK_KEYS]
    chains.append(("delegation.fallback_providers", delegation.get("fallback_providers")))
    for key, value in chains:
        for index, entry in enumerate(_entries(value)):
            if entry.get("provider") != PROVIDER:
                warnings.append(f"{key}[{index}] falls back to provider {entry.get('provider')!r}, outside OpenCode")

    auxiliary = config.get("auxiliary") if isinstance(config.get("auxiliary"), dict) else {}
    for task, settings in sorted(auxiliary.items()):
        provider = settings.get("provider") if isinstance(settings, dict) else None
        if provider in (None, "", "auto", "main"):
            notes.append(f"auxiliary.{task}: provider {provider or 'unset'!r}; Hermes resolves it at runtime")
        else:
            notes.append(f"auxiliary.{task}: provider {provider!r}")

    patterns = [item.lower() for item in forbid if item]
    for path, value in _strings(config):
        if any(pattern in value.lower() for pattern in patterns):
            errors.append(f"{path} references a forbidden endpoint or model (value not shown)")
    return errors, warnings, notes


def default_path():
    home = os.environ.get("HERMES_HOME")
    return (Path(home) if home else Path.home() / ".hermes") / "config.yaml"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=default_path())
    parser.add_argument("--forbid", action="append", default=[],
                        help="Case-insensitive text that must not appear in any value, e.g. a direct CodeLLM host")
    args = parser.parse_args(argv)
    try:
        import yaml
    except ImportError:
        parser.error("PyYAML is required; run this with Hermes's Python environment")
    with open(args.config, encoding="utf-8") as handle:
        errors, warnings, notes = check(yaml.safe_load(handle), args.forbid)
    for label, items in (("ERROR", errors), ("WARN", warnings), ("NOTE", notes)):
        for item in items:
            print(f"{label}: {item}")
    if not args.forbid:
        print("NOTE: pass --forbid <direct CodeLLM host or model> to scan every value")
    print("FAIL" if errors else "OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
