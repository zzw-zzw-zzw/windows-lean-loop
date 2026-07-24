import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.config import ApiConfig, ConfigError
from lean_loop.project_config import (
    load_provider_api_key,
    load_project_api_key,
    load_project_config,
    provider_profiles_view,
    project_config_view,
    save_provider_profile,
    save_project_config,
)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
class ProjectConfigTests(unittest.TestCase):
    def test_subscription_config_requires_model_but_not_api_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config = ApiConfig.for_backend(
                project,
                "codex-subscription",
                model="gpt-5.6-sol",
                reasoning_effort="low",
            )
            self.assertEqual(config.mode, "subscription")
            self.assertEqual(config.api_key, "")
            self.assertEqual(config.model, "gpt-5.6-sol")

    def test_subscription_config_rejects_missing_explicit_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ConfigError, "explicit model"):
                    ApiConfig.for_backend(Path(directory), "claude-subscription")
    def test_persists_settings_and_dpapi_encrypted_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with patch.dict(os.environ, {}, clear=True):
                view = save_project_config(
                    project,
                    {
                        "api_base": "https://relay.example",
                        "model": "gpt-test-sol",
                        "api_mode": "responses",
                        "api_transport": "python",
                        "reasoning_effort": "high",
                        "disable_response_storage": True,
                        "lake": "D:/tools/lake.exe",
                        "timeout_seconds": 600,
                        "max_output_tokens": 12000,
                        "api_timeout_retries": 2,
                        "stream_responses": True,
                        "lsp_local_repair": True,
                        "lsp_local_max_rounds": 3,
                        "lsp_local_max_candidates": 8,
                    },
                    api_key="secret-test-key",
                )
                self.assertTrue(view["api_key_configured"])
                self.assertEqual(view["api_key_source"], "project")
                self.assertEqual(load_project_api_key(project), "secret-test-key")
                self.assertEqual(load_project_config(project)["model"], "gpt-test-sol")
                secret_text = (
                    project / ".lean-agent" / "secrets.json"
                ).read_text(encoding="utf-8")
                self.assertNotIn("secret-test-key", secret_text)
                config = ApiConfig.from_environment(project)
                self.assertEqual(config.api_key, "secret-test-key")
                self.assertEqual(config.model, "gpt-test-sol")
                self.assertEqual(config.api_timeout_retries, 2)
                self.assertEqual(config.api_transport, "python")
                self.assertTrue(config.stream_responses)
                config_view = project_config_view(project)
                self.assertTrue(config_view["lsp_local_repair"])
                self.assertEqual(config_view["lsp_local_max_rounds"], 3)
                self.assertEqual(config_view["lsp_local_max_candidates"], 8)

    def test_can_clear_persisted_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with patch.dict(
                os.environ,
                {
                    "LEAN_AGENT_API_BASE": "https://environment.example",
                    "LEAN_AGENT_API_KEY": "environment-key",
                    "LEAN_AGENT_MODEL": "environment-model",
                },
                clear=True,
            ):
                save_project_config(project, {}, api_key="project-key")
                view = save_project_config(project, {}, clear_api_key=True)
                self.assertEqual(view["api_key_source"], "environment")
                self.assertFalse(
                    (project / ".lean-agent" / "secrets.json").exists()
                )

    def test_persists_independent_deepseek_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with patch.dict(os.environ, {}, clear=True):
                save_project_config(
                    project,
                    {
                        "api_base": "https://relay.example",
                        "model": "gpt-test",
                        "api_mode": "responses",
                    },
                    api_key="gpt-key",
                )
                save_provider_profile(
                    project,
                    "deepseek",
                    {
                        "provider_kind": "deepseek",
                        "api_base": "https://api.deepseek.com",
                        "model": "deepseek-reasoner",
                        "api_mode": "chat-completions",
                        "api_transport": "curl",
                        "reasoning_effort": "high",
                    },
                    api_key="deepseek-key",
                )
                profiles = provider_profiles_view(project)
                self.assertIn("deepseek", profiles)
                self.assertTrue(profiles["deepseek"]["api_key_configured"])
                self.assertEqual(
                    load_provider_api_key(project, "deepseek"), "deepseek-key"
                )
                config = ApiConfig.from_environment(project, "deepseek")
                self.assertEqual(config.provider_kind, "deepseek")
                self.assertEqual(config.api_transport, "curl")
                self.assertEqual(config.api_key, "deepseek-key")
                self.assertEqual(config.endpoint, "https://api.deepseek.com/chat/completions")
                secret_text = (
                    project / ".lean-agent" / "secrets.json"
                ).read_text(encoding="utf-8")
                self.assertNotIn("gpt-key", secret_text)
                self.assertNotIn("deepseek-key", secret_text)

    def test_rejects_unknown_api_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "API_TRANSPORT"):
                with patch.dict(
                    os.environ,
                    {
                        "LEAN_AGENT_API_BASE": "https://relay.example",
                        "LEAN_AGENT_API_KEY": "key",
                        "LEAN_AGENT_MODEL": "model",
                        "LEAN_AGENT_API_TRANSPORT": "unknown",
                    },
                    clear=True,
                ):
                    ApiConfig.from_environment(Path(directory))


if __name__ == "__main__":
    unittest.main()
