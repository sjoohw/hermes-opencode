"""Offline tests for the Hermes profile checker; no Hermes install needed."""
import importlib.util
import unittest

from support import ROOT

spec = importlib.util.spec_from_file_location("check_hermes_config", ROOT / "tools/check_hermes_config.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

GOOD = {"model": {"provider": "hermes-opencode-acp", "default": "opencode-default"},
        "auxiliary": {"vision": {"provider": "gemma-local", "model": "gemma-4"}, "compression": {"provider": "auto"}}}


class ConfigCheckTests(unittest.TestCase):
    def test_bridge_profile_passes_and_lists_auxiliary_routes(self):
        errors, warnings, notes = checker.check(GOOD)
        self.assertEqual((errors, warnings), ([], []))
        self.assertIn("auxiliary.compression: provider 'auto'; Hermes resolves it at runtime", notes)

    def test_other_main_provider_is_an_error(self):
        errors, _, _ = checker.check({"model": {"provider": "custom"}})
        self.assertTrue(errors)

    def test_fallback_chains_outside_the_bridge_are_reported(self):
        config = dict(GOOD, fallback_providers=[{"provider": "custom", "model": "x"}],
                      delegation={"fallback_providers": [{"provider": "hermes-opencode-acp", "model": "y"}]})
        _, warnings, _ = checker.check(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn("fallback_providers[0]", warnings[0])

    def test_forbidden_value_is_reported_by_path_without_the_value(self):
        config = dict(GOOD, providers={"corp": {"base_url": "https://LLM.corp.example/v1", "api_key": "SECRET"}})
        errors, _, _ = checker.check(config, ["llm.corp.example"])
        self.assertEqual(errors, ["providers.corp.base_url references a forbidden endpoint or model (value not shown)"])
        self.assertNotIn("SECRET", " ".join(errors))


if __name__ == "__main__":
    unittest.main()
