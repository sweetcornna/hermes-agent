#!/usr/bin/env python3
"""Tests for the bundled Cornna image_gen plugin.

Every HTTP call is mocked — this suite must never touch the network.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

import plugins.image_gen.cornna as cornna_plugin
from plugins.image_gen.cornna import characters
from plugins.image_gen.cornna import CornnaImageGenProvider

_POST = "plugins.image_gen.cornna.requests.post"

# 1×1 transparent PNG — real bytes, so save_b64_image() writes a real file.
_PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360606060000000050001a5f645400000000049454e"
    "44ae426082"
)


def _b64_png() -> str:
    return base64.b64encode(bytes.fromhex(_PNG_HEX)).decode()


def _b64_png_sized(width: int, height: int) -> str:
    """A real PNG of the given pixel dimensions, base64-encoded.

    Used by the aspect-mismatch backstop tests, which need to control the
    *returned* image's actual dimensions independently of the 1x1 fixture
    every other test uses.
    """
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (128, 128, 128)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _resp(
    *,
    status: int = 200,
    json_body=None,
    json_exc: Exception | None = None,
    text: str = "",
) -> MagicMock:
    """Build a fake ``requests.Response``."""
    mock = MagicMock()
    mock.status_code = status
    mock.text = text
    if json_exc is not None:
        mock.json.side_effect = json_exc
    else:
        mock.json.return_value = json_body if json_body is not None else {}
    return mock


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    """Fake credentials + isolated HERMES_HOME + no ambient config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CORNNA_IMAGE_API_KEY", "fake-image-key-for-tests")
    for var in (
        "CORNNA_IMAGE_BASE_URL",
        "CORNNA_IMAGE_MODEL",
        "CORNNA_IMAGE_SIZE",
        "CORNNA_IMAGE_REFS_BACKEND",
    ):
        monkeypatch.delenv(var, raising=False)
    # config.yaml must not influence resolution unless a test says so.
    monkeypatch.setattr(cornna_plugin, "_load_cornna_image_config", lambda: {})
    # The retry backoff is real seconds in production; the tests exercise the
    # retry *decision*, not the wait.
    monkeypatch.setattr(cornna_plugin, "GENERATION_RETRY_SLEEP_SECONDS", 0)
    yield


# ---------------------------------------------------------------------------
# Provider surface
# ---------------------------------------------------------------------------


class TestProviderSurface:
    def test_name_and_display_name(self):
        provider = CornnaImageGenProvider()
        assert provider.name == "cornna"
        assert provider.display_name == "Cornna"

    def test_list_models_matches_verified_catalog(self):
        ids = [m["id"] for m in CornnaImageGenProvider().list_models()]
        # Exactly the three image models the live /v1/models call returned.
        assert ids == ["gpt-image-2", "gpt-image-1.5", "gpt-image-1"]
        # "image2" is not a real model id on this endpoint.
        assert "image2" not in ids

    def test_default_model(self):
        assert CornnaImageGenProvider().default_model() == "gpt-image-2"

    def test_capabilities_declare_reference_support(self):
        caps = CornnaImageGenProvider().capabilities()
        # Both reference backends are exercised against the endpoint, so the
        # image modality is declared for real rather than withheld.
        assert caps["modalities"] == ["text", "image"]
        # 4 is the enforced ceiling (parity with the old _MAX_REFS)...
        assert caps["max_reference_images"] == cornna_plugin.MAX_REFERENCE_IMAGES == 4
        # ...but it is not the advice: 1-3 is what actually reads well.
        assert caps["recommended_reference_images"] == 3
        assert caps["recommended_reference_images"] < caps["max_reference_images"]
        # Verified-live known limitation: `size` is not honored once a
        # reference is attached (see the module docstring). Must be
        # declared, not silently absorbed by the prompt-instruction
        # workaround.
        assert caps["reference_size_pinned"] is False

    def test_setup_schema_asks_for_the_image_key_only(self):
        schema = CornnaImageGenProvider().get_setup_schema()
        keys = [row["key"] for row in schema["env_vars"]]
        assert keys == ["CORNNA_IMAGE_API_KEY"]
        # Must never point users at the chat credential.
        assert "CORNNA_API_KEY" not in keys


class TestIsAvailable:
    def test_true_with_key(self):
        assert CornnaImageGenProvider().is_available() is True

    def test_false_without_key_and_does_not_raise(self, monkeypatch):
        monkeypatch.delenv("CORNNA_IMAGE_API_KEY", raising=False)
        assert CornnaImageGenProvider().is_available() is False

    def test_blank_key_is_not_available(self, monkeypatch):
        monkeypatch.setenv("CORNNA_IMAGE_API_KEY", "   ")
        assert CornnaImageGenProvider().is_available() is False

    def test_secret_lookup_exception_is_swallowed(self):
        with patch(
            "plugins.image_gen.cornna.get_secret",
            side_effect=RuntimeError("unscoped secret read"),
        ):
            assert CornnaImageGenProvider().is_available() is False

    def test_does_not_fall_back_to_the_chat_key(self, monkeypatch):
        """CORNNA_API_KEY is a different credential — it must not enable this."""
        monkeypatch.delenv("CORNNA_IMAGE_API_KEY", raising=False)
        monkeypatch.setenv("CORNNA_API_KEY", "fake-chat-key")
        assert CornnaImageGenProvider().is_available() is False


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


class TestRequest:
    def test_body_and_endpoint(self):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(
                prompt="a red panda", aspect_ratio="square"
            )

        assert result["success"] is True
        args, kwargs = post.call_args
        assert args[0] == "https://api.cornna.xyz/v1/images/generations"
        assert kwargs["json"] == {
            "model": "gpt-image-2",
            "prompt": "a red panda",
            "n": 1,
            "size": "1024x1024",
        }
        assert kwargs["headers"]["Authorization"] == "Bearer fake-image-key-for-tests"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["timeout"] == cornna_plugin.REQUEST_TIMEOUT

    def test_base_url_env_override_and_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("CORNNA_IMAGE_BASE_URL", "https://mirror.example.test/v1/")
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(prompt="p")
        assert (
            post.call_args[0][0] == "https://mirror.example.test/v1/images/generations"
        )

    def test_base_url_from_config(self, monkeypatch):
        monkeypatch.setattr(
            cornna_plugin,
            "_load_cornna_image_config",
            lambda: {"base_url": "https://cfg.example.test/v1"},
        )
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(prompt="p")
        assert post.call_args[0][0] == "https://cfg.example.test/v1/images/generations"

    def test_model_env_override(self, monkeypatch):
        monkeypatch.setenv("CORNNA_IMAGE_MODEL", "gpt-image-1.5")
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(prompt="p")
        assert post.call_args[1]["json"]["model"] == "gpt-image-1.5"
        assert result["model"] == "gpt-image-1.5"

    def test_model_kwarg_honored_when_known(self):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(prompt="p", model="gpt-image-1")
        assert post.call_args[1]["json"]["model"] == "gpt-image-1"

    def test_unknown_model_kwarg_falls_back_to_default(self):
        """``image_gen.model`` is shared config — a text-model id must not leak."""
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(prompt="p", model="deepseek-chat")
        assert post.call_args[1]["json"]["model"] == "gpt-image-2"

    def test_unknown_kwargs_are_ignored(self):
        """ABC contract: forward-compat kwargs must not raise TypeError."""
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", upscale=True, some_future_flag=1
            )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# aspect_ratio → size mapping
# ---------------------------------------------------------------------------


class TestAspectRatioMapping:
    @pytest.mark.parametrize(
        "aspect,size",
        [
            ("square", "1024x1024"),  # verified against the live endpoint
            ("landscape", "1536x1024"),  # unverified
            ("portrait", "1024x1536"),  # unverified
        ],
    )
    def test_mapping(self, aspect, size):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(prompt="p", aspect_ratio=aspect)
        assert post.call_args[1]["json"]["size"] == size
        assert result["aspect_ratio"] == aspect

    def test_garbage_aspect_ratio_coerces_to_default(self):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(prompt="p", aspect_ratio="17:3")
        assert result["aspect_ratio"] == "landscape"
        assert post.call_args[1]["json"]["size"] == "1536x1024"

    def test_unverified_size_400_retries_at_verified_size(self):
        """Only 1024x1024 is verified — a 400 on an unverified size falls back."""
        rejected = _resp(
            status=400,
            json_body={"error": {"message": "unsupported size"}},
            text="unsupported size",
        )
        accepted = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, side_effect=[rejected, accepted]) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="landscape"
            )

        assert result["success"] is True
        assert post.call_count == 2
        assert post.call_args_list[0][1]["json"]["size"] == "1536x1024"
        assert post.call_args_list[1][1]["json"]["size"] == "1024x1024"
        assert result["size"] == "1024x1024"
        assert result["size_fallback"] is True
        assert result["requested_size"] == "1536x1024"

    def test_fallback_is_attempted_only_once(self):
        rejected = _resp(status=400, text="unsupported size")
        with patch(_POST, side_effect=[rejected, rejected]) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="portrait"
            )
        assert post.call_count == 2
        assert result["success"] is False
        assert result["error_type"] == "api_error"

    def test_verified_size_400_does_not_retry(self):
        rejected = _resp(status=400, text="bad prompt")
        with patch(_POST, side_effect=[rejected]) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="square"
            )
        assert post.call_count == 1
        assert result["success"] is False

    def test_operator_pinned_size_is_never_rewritten(self, monkeypatch):
        monkeypatch.setenv("CORNNA_IMAGE_SIZE", "2048x2048")
        rejected = _resp(status=400, text="unsupported size")
        with patch(_POST, side_effect=[rejected]) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="landscape"
            )
        assert post.call_count == 1
        assert post.call_args[1]["json"]["size"] == "2048x2048"
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


class TestSuccessResponse:
    def test_b64_json_is_written_to_a_readable_local_file(self):
        """b64 → an absolute path that downstream consumers can open."""
        resp = _resp(
            json_body={
                "data": [{"b64_json": _b64_png(), "revised_prompt": "a nicer prompt"}],
                "usage": {"total_tokens": 42},
            }
        )
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="square"
            )

        assert result["success"] is True
        assert result["provider"] == "cornna"
        assert result["model"] == "gpt-image-2"
        assert result["modality"] == "text"
        assert result["revised_prompt"] == "a nicer prompt"
        assert result["usage"] == {"total_tokens": 42}

        path = result["image"]
        assert os.path.isabs(path)
        assert os.path.isfile(path)
        with open(path, "rb") as fh:
            assert fh.read() == bytes.fromhex(_PNG_HEX)

    def test_result_shape_is_accepted_by_qzone_load_image_reference(self):
        """The downstream consumer must accept what we hand back verbatim."""
        from plugins.qzone.publish import _load_image_reference

        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(prompt="p")

        raw, filename = _load_image_reference(result["image"])
        assert raw == bytes.fromhex(_PNG_HEX)
        assert filename

    def test_url_response_is_cached_locally(self, tmp_path):
        resp = _resp(json_body={"data": [{"url": "https://cdn.example.test/a.png"}]})
        cached = tmp_path / "cached.png"
        cached.write_bytes(bytes.fromhex(_PNG_HEX))
        with (
            patch(_POST, return_value=resp),
            patch(
                "plugins.image_gen.cornna.save_url_image",
                return_value=cached,
            ),
        ):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is True
        assert result["image"] == str(cached)

    def test_url_cache_failure_is_not_reported_as_success(self):
        resp = _resp(json_body={"data": [{"url": "https://cdn.example.test/a.png"}]})
        with (
            patch(_POST, return_value=resp),
            patch(
                "plugins.image_gen.cornna.save_url_image",
                side_effect=RuntimeError("404 from CDN"),
            ),
        ):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "io_error"

    def test_invalid_cached_url_image_is_not_reported_as_success(self, tmp_path):
        resp = _resp(json_body={"data": [{"url": "https://cdn.example.test/a.png"}]})
        cached = tmp_path / "cached.png"
        cached.write_bytes(b"not an image")
        with (
            patch(_POST, return_value=resp),
            patch("plugins.image_gen.cornna.save_url_image", return_value=cached),
        ):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "invalid_response"

    def test_b64_wins_over_url(self):
        resp = _resp(
            json_body={
                "data": [
                    {"b64_json": _b64_png(), "url": "https://cdn.example.test/a.png"}
                ]
            }
        )
        with (
            patch(_POST, return_value=resp),
            patch(
                "plugins.image_gen.cornna.save_url_image",
                side_effect=AssertionError("must not download when b64 is present"),
            ),
        ):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert os.path.isfile(result["image"])


# ---------------------------------------------------------------------------
# Failure paths — all structured, never a bare exception
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("CORNNA_IMAGE_API_KEY", raising=False)
        with patch(_POST, side_effect=AssertionError("must not call the network")):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "auth_required"
        assert "CORNNA_IMAGE_API_KEY" in result["error"]
        assert result["image"] is None

    def test_empty_prompt(self):
        with patch(_POST, side_effect=AssertionError("must not call the network")):
            result = CornnaImageGenProvider().generate(prompt="   ")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_unreadable_source_image_is_structured(self):
        """image_url is supported now — but an unreadable one still fails clean."""
        with patch(_POST, side_effect=AssertionError("must not call the network")):
            result = CornnaImageGenProvider().generate(
                prompt="p", image_url="/nonexistent/definitely-not-here.png"
            )
        assert result["success"] is False
        assert result["error_type"] == "io_error"

    def test_http_error_is_structured(self):
        resp = _resp(
            status=401,
            json_body={"error": {"message": "Invalid API key"}},
            text="Unauthorized",
        )
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "401" in result["error"]
        assert "Invalid API key" in result["error"]
        assert result["model"] == "gpt-image-2"

    def test_http_error_without_json_body_uses_text(self):
        resp = _resp(status=502, json_exc=ValueError("no json"), text="bad gateway")
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "bad gateway" in result["error"]

    def test_non_json_success_body_is_structured(self):
        resp = _resp(status=200, json_exc=ValueError("Expecting value"))
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "invalid_response"
        assert result["image"] is None

    @pytest.mark.parametrize(
        "body",
        [{"data": []}, {}, {"data": None}],
    )
    def test_empty_data_is_structured(self, body):
        with patch(_POST, return_value=_resp(json_body=body)):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_entry_without_b64_or_url_is_structured(self):
        with patch(_POST, return_value=_resp(json_body={"data": [{"junk": 1}]})):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_invalid_b64_image_is_not_reported_as_success(self):
        bad_image = base64.b64encode(b"not an image").decode()
        with patch(
            _POST, return_value=_resp(json_body={"data": [{"b64_json": bad_image}]})
        ):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "invalid_response"

    def test_save_failure_is_structured(self):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with (
            patch(_POST, return_value=resp),
            patch(
                "plugins.image_gen.cornna.save_b64_image",
                side_effect=OSError("disk full"),
            ),
        ):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "io_error"

    def test_timeout(self):
        import requests as req_lib

        with patch(_POST, side_effect=req_lib.Timeout()):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "timeout"

    def test_connection_error(self):
        import requests as req_lib

        with patch(_POST, side_effect=req_lib.ConnectionError("dns")):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "connection_error"

    def test_generic_request_exception(self):
        import requests as req_lib

        with patch(_POST, side_effect=req_lib.RequestException("boom")):
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is False
        assert result["error_type"] == "api_error"

    def test_retryable_text_failure_retries_then_succeeds(self):
        failed = _resp(status=503, text="upstream unavailable")
        accepted = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, side_effect=[failed, accepted]) as post:
            result = CornnaImageGenProvider().generate(prompt="p")
        assert result["success"] is True
        assert post.call_count == 2


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_hands_a_provider_to_the_context(self):
        ctx = MagicMock()
        cornna_plugin.register(ctx)
        ctx.register_image_gen_provider.assert_called_once()
        provider = ctx.register_image_gen_provider.call_args[0][0]
        assert isinstance(provider, CornnaImageGenProvider)

    def test_manifest_declares_the_image_key(self):
        import yaml

        manifest_path = os.path.join(
            os.path.dirname(cornna_plugin.__file__), "plugin.yaml"
        )
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = yaml.safe_load(fh)
        assert manifest["name"] == "cornna"
        assert manifest["kind"] == "backend"
        assert manifest["requires_env"] == ["CORNNA_IMAGE_API_KEY"]


# ---------------------------------------------------------------------------
# Wiring — bundled auto-load + the qzone call chain
# ---------------------------------------------------------------------------


class TestWiring:
    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from agent import image_gen_registry

        image_gen_registry._reset_for_tests()
        yield
        image_gen_registry._reset_for_tests()

    def test_bundled_plugin_autoloads_and_registers(self):
        """``kind: backend`` + bundled ⇒ no opt-in needed in plugins.enabled."""
        from agent import image_gen_registry
        from hermes_cli.plugins import PluginManager

        mgr = PluginManager()
        mgr.discover_and_load()

        assert "image_gen/cornna" in mgr._plugins
        loaded = mgr._plugins["image_gen/cornna"]
        assert loaded.manifest.source == "bundled"
        assert loaded.manifest.kind == "backend"

        # In a single-process run over the whole suite, an earlier test can
        # leave ``agent.image_gen_provider`` imported under two module
        # identities, after which register_provider()'s isinstance() check
        # rejects EVERY image_gen plugin (the bundled openai one included —
        # see tests/hermes_cli/test_plugin_scanner_recursion.py, which fails
        # the same way). The canonical runner isolates each file in its own
        # subprocess (scripts/run_tests.sh), where this passes. Use the
        # bundled openai plugin as the control: if it can't register either,
        # the session is poisoned and this assertion proves nothing.
        reference = mgr._plugins.get("image_gen/openai")
        if reference is not None and reference.enabled is not True:
            pytest.skip(
                "plugin registration is broken session-wide "
                f"(control plugin image_gen/openai: {reference.error}); "
                "run this file in isolation"
            )

        assert loaded.enabled is True, f"error: {loaded.error}"
        assert image_gen_registry.get_provider("cornna") is not None

    def test_qzone_chain_end_to_end(self, monkeypatch):
        """image_gen.provider=cornna ⇒ the gate opens and qzone gets bytes."""
        import json

        from agent import image_gen_registry
        from hermes_cli import plugins as plugins_module
        from plugins.qzone.publish import _load_image_reference
        from tools import image_generation_tool

        image_gen_registry.register_provider(CornnaImageGenProvider())
        monkeypatch.setattr(image_generation_tool, "check_fal_api_key", lambda: False)
        monkeypatch.setattr(
            image_generation_tool, "_read_configured_image_provider", lambda: "cornna"
        )
        monkeypatch.setattr(
            image_generation_tool, "_read_configured_image_model", lambda: None
        )
        monkeypatch.setattr(
            plugins_module, "_ensure_plugins_discovered", lambda *a, **k: None
        )

        # The gate qzone's _generate_image checks before calling the tool.
        assert image_generation_tool.check_image_generation_requirements() is True

        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp):
            raw = image_generation_tool._handle_image_generate({
                "prompt": "配图",
                "aspect_ratio": "square",
            })

        payload = json.loads(raw) if isinstance(raw, str) else raw
        assert payload["success"] is True, payload
        assert not payload.get("error")
        raw_bytes, filename = _load_image_reference(payload["image"])
        assert raw_bytes == bytes.fromhex(_PNG_HEX)
        assert filename


# ---------------------------------------------------------------------------
# Character 立绘 registry
# ---------------------------------------------------------------------------


class TestCharacterRegistry:
    def test_all_eleven_keys_carried_over(self):
        assert set(characters.CHARACTER_KEYS) == {
            "grantley",
            "algo",
            "oscar",
            "diedrich",
            "paul",
            "theo",
            "julius",
            "hermann",
            "helio",
            "shayat",
            "bating",
        }
        assert characters.CHARACTER_KEYS["grantley"] == "02_grantley_bell.png"

    def test_dir_defaults_to_profile_aware_characters_directory(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "local-home"))
        assert characters.character_dir() == tmp_path / "local-home" / "characters"

    def test_dir_uses_the_same_subtree_for_a_service_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "srv" / "hermes"))
        assert characters.character_dir() == tmp_path / "srv" / "hermes" / "characters"

    def test_dir_matches_the_production_hermes_home_contract(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "/opt/hermes/data")
        assert characters.character_dir() == Path("/opt/hermes/data/characters")

    def test_dir_config_beats_hermes_home(self, tmp_path):
        got = characters.character_dir({"character_dir": str(tmp_path / "cfg")})
        assert got == tmp_path / "cfg"

    def test_known_name_resolves_under_the_directory(self, tmp_path):
        path = characters.character_path("grantley", directory=tmp_path)
        assert path == tmp_path / "02_grantley_bell.png"

    def test_name_is_case_and_whitespace_forgiving(self, tmp_path):
        path = characters.character_path("  Grantley ", directory=tmp_path)
        assert path == tmp_path / "02_grantley_bell.png"

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "../../../../../../etc/passwd",
            "/etc/passwd",
            "grantley/../../../etc/passwd",
            "02_grantley_bell.png",  # the real filename is still not a key
            "",
            None,
        ],
    )
    def test_traversal_is_rejected_before_the_filesystem_is_touched(
        self, hostile, tmp_path
    ):
        """The allowlist is checked first — a hostile name never becomes a path."""
        with (
            patch(
                "pathlib.Path.is_file",
                side_effect=AssertionError("must not stat anything for a bad name"),
            ),
            patch(
                "pathlib.Path.read_bytes",
                side_effect=AssertionError("must not read anything for a bad name"),
            ),
        ):
            with pytest.raises(characters.UnknownCharacterError):
                characters.character_path(hostile, directory=tmp_path)
            with pytest.raises(characters.UnknownCharacterError):
                characters.resolve_character_file(hostile, directory=tmp_path)

    def test_unknown_name_error_lists_the_valid_keys(self, tmp_path):
        with pytest.raises(characters.UnknownCharacterError) as excinfo:
            characters.character_path("mallory", directory=tmp_path)
        message = str(excinfo.value)
        assert "mallory" in message
        assert "grantley" in message and "bating" in message

    def test_missing_asset_error_is_diagnosable(self, tmp_path):
        with pytest.raises(characters.MissingCharacterAssetError) as excinfo:
            characters.resolve_character_file("algo", directory=tmp_path)
        message = str(excinfo.value)
        assert "algo" in message  # which short name
        assert str(tmp_path / "01_algo_northrop.png") in message  # expected path
        assert "$HERMES_HOME/characters" in message
        assert "image_gen.cornna.character_dir" in message

    def test_missing_asset_is_reported_to_the_provider(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            characters.load_character_image("theo", directory=tmp_path)

    def test_present_asset_loads_with_its_mime(self, tmp_path):
        (tmp_path / "06_theo_prince.png").write_bytes(bytes.fromhex(_PNG_HEX))
        data, filename, mime = characters.load_character_image(
            "theo", directory=tmp_path
        )
        assert data == bytes.fromhex(_PNG_HEX)
        assert filename == "06_theo_prince.png"
        assert mime == "image/png"

    def test_data_url_shape(self, tmp_path):
        (tmp_path / "06_theo_prince.png").write_bytes(bytes.fromhex(_PNG_HEX))
        url = characters.character_data_url("theo", directory=tmp_path)
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == bytes.fromhex(_PNG_HEX)


class TestAvailableCharacters:
    def test_empty_when_nothing_is_deployed(self, tmp_path):
        assert characters.available_characters(directory=tmp_path) == []

    def test_reports_only_what_is_on_disk(self, tmp_path):
        (tmp_path / "02_grantley_bell.png").write_bytes(bytes.fromhex(_PNG_HEX))
        (tmp_path / "05_paul_pfizner.png").write_bytes(b"not an image")
        (tmp_path / "INVALID-q版推测立绘.png").write_bytes(bytes.fromhex(_PNG_HEX))
        assert characters.available_characters(directory=tmp_path) == [
            "grantley",
        ]

    def test_provider_exposes_it(self, tmp_path, monkeypatch):
        directory = tmp_path / "characters"
        directory.mkdir(parents=True)
        (directory / "10_shayat.png").write_bytes(bytes.fromhex(_PNG_HEX))
        assert CornnaImageGenProvider().available_characters() == ["shayat"]


class TestCharacterAssetValidation:
    def test_corrupt_named_asset_is_not_available_or_loadable(self, tmp_path):
        asset = tmp_path / "02_grantley_bell.png"
        asset.write_bytes(b"not an image")

        assert characters.available_characters(directory=tmp_path) == []
        with pytest.raises(characters.InvalidCharacterAssetError):
            characters.load_character_image("grantley", directory=tmp_path)


# ---------------------------------------------------------------------------
# Reference-anchored generation
# ---------------------------------------------------------------------------


@pytest.fixture
def character_dir(tmp_path, monkeypatch):
    """A characters/ directory holding grantley + algo."""
    directory = tmp_path / "characters"
    directory.mkdir(parents=True)
    for filename in (
        "02_grantley_bell.png",
        "01_algo_northrop.png",
        "03_oscar_lawrence.png",
        "04_diedrich_olsen.png",
        "05_paul_pfizner.png",
    ):
        (directory / filename).write_bytes(bytes.fromhex(_PNG_HEX))
    return directory


def _responses_body(b64: str | None = None) -> dict:
    """A /responses body carrying an image_generation_call result."""
    return {
        "output": [
            {"type": "reasoning", "id": "rs_1"},
            {
                "type": "image_generation_call",
                "id": "ig_1",
                "status": "completed",
                "result": b64 if b64 is not None else _b64_png(),
            },
        ],
        "usage": {"total_tokens": 99},
    }


class TestRefsBackendSelection:
    def test_default_is_responses(self):
        assert cornna_plugin._resolve_refs_backend({}) == "responses"
        assert cornna_plugin.DEFAULT_REFS_BACKEND == "responses"

    def test_config_selects_edits(self):
        assert cornna_plugin._resolve_refs_backend({"refs_backend": "edits"}) == "edits"

    def test_env_beats_config(self, monkeypatch):
        monkeypatch.setenv("CORNNA_IMAGE_REFS_BACKEND", "EDITS")
        assert (
            cornna_plugin._resolve_refs_backend({"refs_backend": "responses"})
            == "edits"
        )

    def test_unknown_value_raises_rather_than_quietly_picking_one(self):
        with pytest.raises(ValueError) as excinfo:
            cornna_plugin._resolve_refs_backend({"refs_backend": "magic"})
        assert "magic" in str(excinfo.value)
        assert "responses" in str(excinfo.value) and "edits" in str(excinfo.value)

    def test_misconfiguration_surfaces_as_a_structured_error(
        self, character_dir, monkeypatch
    ):
        monkeypatch.setattr(
            cornna_plugin,
            "_load_cornna_image_config",
            lambda: {"refs_backend": "magic"},
        )
        with patch(_POST, side_effect=AssertionError("must not call the network")):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_configuration"


class TestRefsViaResponses:
    def test_request_shape(self, character_dir):
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(
                prompt="窗边喝茶",
                aspect_ratio="landscape",
                reference_characters=["grantley"],
            )

        assert result["success"] is True, result
        url = post.call_args[0][0]
        body = post.call_args[1]["json"]
        assert url == "https://api.cornna.xyz/v1/responses"
        assert post.call_args[1]["timeout"] == cornna_plugin.REFS_REQUEST_TIMEOUT

        # Host chat model issues the tool call; the image model draws.
        assert body["model"] == "gpt-5.5"
        assert body["store"] is False
        assert body["instructions"]
        assert body["tools"] == [
            {
                "type": "image_generation",
                "model": "gpt-image-2",
                "size": "1536x1024",
                "quality": "medium",
                "output_format": "png",
                "background": "opaque",
            }
        ]
        assert body["tool_choice"] == {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "image_generation"}],
        }

        content = body["input"][0]["content"]
        assert body["input"][0]["role"] == "user"
        assert content[0]["type"] == "input_text"
        assert "窗边喝茶" in content[0]["text"]
        assert "strictly match their species" in content[0]["text"]
        assert "grantley" in content[0]["text"]
        assert content[1] == {
            "type": "input_image",
            "image_url": "data:image/png;base64," + _b64_png(),
            "detail": "high",
        }

    def test_result_is_saved_and_reported_as_the_image_modality(self, character_dir):
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley", "algo"]
            )
        assert result["modality"] == "image"
        assert result["refs_backend"] == "responses"
        assert result["reference_images"] == 2
        assert result["characters"] == ["grantley", "algo"]
        assert result["usage"] == {"total_tokens": 99}
        assert os.path.isfile(result["image"])
        with open(result["image"], "rb") as fh:
            assert fh.read() == bytes.fromhex(_PNG_HEX)

    def test_last_non_empty_image_generation_call_wins(self):
        body = {
            "output": [
                {"type": "image_generation_call", "result": ""},
                {"type": "image_generation_call", "result": "AAA"},
                {"type": "image_generation_call", "result": "BBB"},
            ]
        }
        assert cornna_plugin._image_b64_from_responses_output(body) == "BBB"

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"output": []},
            {"output": None},
            {"output": [{"type": "message", "content": []}]},
            {"output": [{"type": "image_generation_call", "result": ""}]},
        ],
    )
    def test_bodies_without_an_image_yield_none(self, body):
        assert cornna_plugin._image_b64_from_responses_output(body) is None

    def test_a_prose_only_response_is_an_empty_response_error(self, character_dir):
        resp = _resp(json_body={"output": [{"type": "message", "content": []}]})
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert result["success"] is False
        assert result["error_type"] == "empty_response"


class TestRefsViaEdits:
    @pytest.fixture(autouse=True)
    def _use_edits(self, monkeypatch):
        monkeypatch.setenv("CORNNA_IMAGE_REFS_BACKEND", "edits")

    def test_request_shape(self, character_dir):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(
                prompt="窗边喝茶",
                aspect_ratio="portrait",
                reference_characters=["grantley"],
            )

        assert result["success"] is True, result
        assert post.call_args[0][0] == "https://api.cornna.xyz/v1/images/edits"
        kwargs = post.call_args[1]
        assert kwargs["timeout"] == cornna_plugin.REFS_REQUEST_TIMEOUT

        # multipart: no JSON body, references in the `image` field.
        assert kwargs["json"] is None
        assert kwargs["data"] == {
            "model": "gpt-image-2",
            "prompt": kwargs["data"]["prompt"],
            "n": "1",
            "size": "1024x1536",
        }
        assert "窗边喝茶" in kwargs["data"]["prompt"]
        assert kwargs["files"] == [
            ("image", ("02_grantley_bell.png", bytes.fromhex(_PNG_HEX), "image/png"))
        ]
        # requests must own the multipart boundary.
        assert "Content-Type" not in kwargs["headers"]
        assert kwargs["headers"]["Authorization"] == "Bearer fake-image-key-for-tests"

    def test_multiple_references_are_repeated_image_parts(self, character_dir):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley", "algo"]
            )
        fields = [name for name, _payload in post.call_args[1]["files"]]
        assert fields == ["image", "image"]

    def test_result_reports_the_edits_backend(self, character_dir):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert result["refs_backend"] == "edits"
        assert result["modality"] == "image"
        assert os.path.isfile(result["image"])


class TestReferenceSourceForms:
    def test_character_prefix_in_reference_image_urls(self, character_dir):
        """The model-reachable spelling — a name, never a path."""
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_image_urls=["character:grantley"]
            )
        assert result["characters"] == ["grantley"]
        assert (
            post.call_args[1]["json"]["input"][0]["content"][1]["type"] == "input_image"
        )

    def test_characters_alias_is_accepted(self, character_dir):
        """`characters` is what the old tool's schema called the field."""
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(prompt="p", characters=["algo"])
        assert result["characters"] == ["algo"]

    def test_hostile_character_name_never_reaches_the_disk(self, character_dir):
        with (
            patch(_POST, side_effect=AssertionError("must not call the network")),
            patch(
                "pathlib.Path.is_file",
                side_effect=AssertionError("must not stat a hostile name"),
            ),
            patch(
                "pathlib.Path.read_bytes",
                side_effect=AssertionError("must not read a hostile name"),
            ),
        ):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_image_urls=["character:../../etc/passwd"]
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"
        assert "unknown character short name" in result["error"]

    def test_missing_character_asset_falls_back_to_plain_generation(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        text_response = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=text_response) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert result["success"] is True
        assert result["modality"] == "text"
        assert result["reference_fallback"] == "missing_character_assets"
        assert result["missing_characters"] == ["grantley"]
        assert post.call_args[0][0].endswith("/images/generations")

    def test_local_path_reference(self, tmp_path):
        source = tmp_path / "source.png"
        source.write_bytes(bytes.fromhex(_PNG_HEX))
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", image_url=str(source)
            )
        assert result["success"] is True
        assert result.get("characters") is None
        assert post.call_args[1]["json"]["input"][0]["content"][1][
            "image_url"
        ].startswith("data:image/png;base64,")

    def test_non_image_local_reference_is_rejected_before_generation(self, tmp_path):
        source = tmp_path / "source.png"
        source.write_bytes(b"not an image")
        with patch(_POST, side_effect=AssertionError("must not generate")):
            result = CornnaImageGenProvider().generate(
                prompt="p", image_url=str(source)
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_reference"

    def test_corrupt_character_reference_is_rejected_before_generation(self, tmp_path):
        directory = tmp_path / "characters"
        directory.mkdir(parents=True)
        (directory / "02_grantley_bell.png").write_bytes(b"not an image")
        with patch(_POST, side_effect=AssertionError("must not generate")):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_reference"

    def test_data_url_reference(self):
        resp = _resp(json_body=_responses_body())
        data_url = "data:image/jpeg;base64," + _b64_png()
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_image_urls=[data_url]
            )
        assert result["success"] is True
        assert post.call_args[1]["json"]["input"][0]["content"][1]["image_url"] == (
            "data:image/png;base64," + _b64_png()
        )

    def test_http_reference_is_downloaded(self):
        fetched = MagicMock()
        fetched.content = bytes.fromhex(_PNG_HEX)
        fetched.headers = {"Content-Type": "image/png"}
        resp = _resp(json_body=_responses_body())
        with (
            patch(_POST, return_value=resp),
            patch("plugins.image_gen.cornna.requests.get", return_value=fetched) as get,
        ):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_image_urls=["https://cdn.example.test/ref.png"]
            )
        assert result["success"] is True
        assert get.call_args[0][0] == "https://cdn.example.test/ref.png"

    def test_non_image_data_reference_is_rejected_before_generation(self):
        data_url = "data:image/png;base64," + base64.b64encode(b"not an image").decode()
        with patch(_POST, side_effect=AssertionError("must not generate")):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_image_urls=[data_url]
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_reference"

    def test_reference_download_timeout_is_structured(self):
        import requests as req_lib

        with patch(
            "plugins.image_gen.cornna.requests.get", side_effect=req_lib.Timeout()
        ):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_image_urls=["https://cdn.example.test/ref.png"]
            )
        assert result["success"] is False
        assert result["error_type"] == "timeout"

    def test_no_references_still_takes_the_text_to_image_path(self):
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(prompt="p")
        assert post.call_args[0][0].endswith("/images/generations")
        assert result["modality"] == "text"


class TestReferenceCountCap:
    def test_at_the_cap_is_allowed(self, character_dir):
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p",
                reference_characters=["grantley", "algo", "oscar", "diedrich"],
            )
        assert result["success"] is True
        assert result["reference_images"] == 4

    def test_over_the_cap_is_refused_before_any_disk_or_network(self, character_dir):
        with (
            patch(_POST, side_effect=AssertionError("must not call the network")),
            patch(
                "pathlib.Path.read_bytes",
                side_effect=AssertionError("must not read a 立绘 to count them"),
            ),
        ):
            result = CornnaImageGenProvider().generate(
                prompt="p",
                reference_characters=[
                    "grantley",
                    "algo",
                    "oscar",
                    "diedrich",
                    "paul",
                ],
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"
        assert "5" in result["error"] and "4" in result["error"]
        # The old implementation's actual reason, not just a number.
        assert "prompt adherence" in result["error"]

    def test_the_cap_counts_every_reference_form_together(self, character_dir):
        with patch(_POST, side_effect=AssertionError("must not call the network")):
            result = CornnaImageGenProvider().generate(
                prompt="p",
                image_url="https://cdn.example.test/a.png",
                reference_image_urls=["https://cdn.example.test/b.png"],
                reference_characters=["grantley", "algo", "oscar"],
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_over_the_recommendation_warns_but_proceeds(self, character_dir, caplog):
        resp = _resp(json_body=_responses_body())
        with caplog.at_level("WARNING"), patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p",
                reference_characters=["grantley", "algo", "oscar", "diedrich"],
            )
        assert result["success"] is True
        assert "recommended" in caplog.text


class TestRefsRetry:
    def test_a_5xx_is_retried(self, character_dir):
        flaky = _resp(status=503, text="upstream unavailable")
        good = _resp(json_body=_responses_body())
        with patch(_POST, side_effect=[flaky, good]) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert result["success"] is True
        assert post.call_count == 2

    def test_a_4xx_is_not_retried(self, character_dir):
        rejected = _resp(
            status=400, json_body={"error": {"message": "bad prompt"}}, text="bad"
        )
        with patch(_POST, side_effect=[rejected]) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert post.call_count == 1
        assert result["success"] is False
        assert "bad prompt" in result["error"]

    def test_an_empty_result_is_retried_then_surfaced(self, character_dir):
        empty = _resp(json_body={"output": []})
        with patch(_POST, return_value=empty) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert post.call_count == cornna_plugin.GENERATION_MAX_ATTEMPTS
        assert result["error_type"] == "empty_response"

    def test_a_timeout_is_retried_then_surfaced(self, character_dir):
        import requests as req_lib

        with patch(_POST, side_effect=req_lib.Timeout()) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert post.call_count == cornna_plugin.GENERATION_MAX_ATTEMPTS
        assert result["error_type"] == "timeout"

    def test_the_api_key_never_appears_in_an_error(self, character_dir):
        rejected = _resp(status=401, json_body={"error": {"message": "nope"}})
        with patch(_POST, side_effect=[rejected]):
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert "fake-image-key-for-tests" not in json.dumps(result)


class TestAnchoringPreamble:
    def test_named_characters_get_the_anchoring_instruction(self, character_dir):
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(
                prompt="喝茶", reference_characters=["grantley", "algo"]
            )
        text = post.call_args[1]["json"]["input"][0]["content"][0]["text"]
        assert text.startswith("You are given character reference portraits.")
        assert "Reference characters provided in order: grantley, algo." in text
        assert text.endswith("Scene:\n\n喝茶")

    def test_a_plain_image_edit_keeps_its_prompt_verbatim(self, tmp_path):
        """No character named ⇒ no species/fur instruction to steer it wrong."""
        source = tmp_path / "photo.jpg"
        source.write_bytes(bytes.fromhex(_PNG_HEX))
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(
                prompt="make this look like winter", image_url=str(source)
            )
        text = post.call_args[1]["json"]["input"][0]["content"][0]["text"]
        assert text == "make this look like winter"


class TestEditsRetryResendsTheReferenceBytes:
    def test_a_retried_multipart_post_still_carries_the_image(
        self, character_dir, monkeypatch
    ):
        """Raw bytes, not file handles — a handle would be at EOF on retry."""
        monkeypatch.setenv("CORNNA_IMAGE_REFS_BACKEND", "edits")
        flaky = _resp(status=502, text="bad gateway")
        good = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, side_effect=[flaky, good]) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", reference_characters=["grantley"]
            )
        assert result["success"] is True
        assert post.call_count == 2
        for call in post.call_args_list:
            assert call[1]["files"] == [
                (
                    "image",
                    ("02_grantley_bell.png", bytes.fromhex(_PNG_HEX), "image/png"),
                )
            ]


# ---------------------------------------------------------------------------
# Orientation instruction (the fix for aspect_ratio being ignored on refs)
# ---------------------------------------------------------------------------


class TestAspectInstruction:
    """The endpoint ignores `size` once a reference is attached (verified
    live — see the module docstring). The prompt-level orientation
    instruction is the workaround that actually steers it; these tests
    pin the instruction's presence, its placement, and its wiring into
    both refs backends.
    """

    @pytest.mark.parametrize(
        "aspect,phrase",
        [
            ("landscape", "landscape-oriented"),
            ("square", "perfectly square"),
            ("portrait", "portrait-oriented"),
        ],
    )
    def test_named_character_prompt_carries_the_orientation_instruction(
        self, character_dir, aspect, phrase
    ):
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp) as post:
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio=aspect, reference_characters=["grantley"]
            )
        assert result["success"] is True
        text = post.call_args[1]["json"]["input"][0]["content"][0]["text"]
        assert phrase in text
        # Ahead of the scene, same as the character-anchoring preamble —
        # and the anchoring preamble's own start/end checks
        # (TestAnchoringPreamble) must still hold with this added.
        assert text.index(phrase) < text.index("Scene:\n\n")
        assert text.startswith("You are given character reference portraits.")

    def test_edits_backend_prompt_also_carries_the_instruction(
        self, character_dir, monkeypatch
    ):
        """The two backends share one prompt-building path — confirm it
        actually reaches the edits multipart body too."""
        monkeypatch.setenv("CORNNA_IMAGE_REFS_BACKEND", "edits")
        resp = _resp(json_body={"data": [{"b64_json": _b64_png()}]})
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="landscape", reference_characters=["grantley"]
            )
        assert "landscape-oriented" in post.call_args[1]["data"]["prompt"]

    def test_plain_image_edit_still_gets_no_orientation_instruction(self, tmp_path):
        """Unnamed reference ⇒ still verbatim (parallels TestAnchoringPreamble's
        species/fur guard: forcing an orientation on an "edit this photo"
        call fights that intent the same way forcing a species would)."""
        source = tmp_path / "photo.jpg"
        source.write_bytes(bytes.fromhex(_PNG_HEX))
        resp = _resp(json_body=_responses_body())
        with patch(_POST, return_value=resp) as post:
            CornnaImageGenProvider().generate(
                prompt="make this look like winter",
                aspect_ratio="landscape",
                image_url=str(source),
            )
        text = post.call_args[1]["json"]["input"][0]["content"][0]["text"]
        assert text == "make this look like winter"


# ---------------------------------------------------------------------------
# Aspect-mismatch backstop — must never silently return the wrong shape
# ---------------------------------------------------------------------------


class TestAspectMismatchBackstop:
    def test_matching_aspect_is_not_flagged(self, character_dir, caplog):
        resp = _resp(json_body=_responses_body(b64=_b64_png_sized(1536, 1024)))
        with caplog.at_level("WARNING"), patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="landscape", reference_characters=["grantley"]
            )
        assert result["success"] is True
        assert result.get("aspect_mismatch") is None
        assert result["actual_size"] == "1536x1024"
        assert "mismatch" not in caplog.text

    def test_mismatched_aspect_is_flagged_and_logged(self, character_dir, caplog):
        """The exact failure mode this fix targets: landscape requested,
        but the endpoint hands back a portrait image (matching the
        reference's own orientation). Must never be silent."""
        resp = _resp(json_body=_responses_body(b64=_b64_png_sized(962, 1634)))
        with caplog.at_level("WARNING"), patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="landscape", reference_characters=["grantley"]
            )
        assert result["success"] is True  # still a usable image...
        assert result["aspect_mismatch"] is True  # ...but flagged, not silent.
        assert result["actual_size"] == "962x1634"
        assert "aspect mismatch" in caplog.text
        assert "landscape" in caplog.text
        assert "portrait" in caplog.text

    def test_mismatch_backstop_also_covers_the_edits_backend(
        self, character_dir, monkeypatch, caplog
    ):
        monkeypatch.setenv("CORNNA_IMAGE_REFS_BACKEND", "edits")
        resp = _resp(json_body={"data": [{"b64_json": _b64_png_sized(962, 1634)}]})
        with caplog.at_level("WARNING"), patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="square", reference_characters=["grantley"]
            )
        assert result["aspect_mismatch"] is True
        assert result["actual_size"] == "962x1634"
        assert "aspect mismatch" in caplog.text

    def test_near_square_is_within_tolerance(self, character_dir):
        # 1000x1024 is close enough to 1:1 to still read as "square".
        resp = _resp(json_body=_responses_body(b64=_b64_png_sized(1000, 1024)))
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="square", reference_characters=["grantley"]
            )
        assert result.get("aspect_mismatch") is None
        assert result["actual_size"] == "1000x1024"

    def test_unmeasurable_response_image_is_an_invalid_response(self, character_dir):
        """Base64 alone is not enough for a provider success response."""
        garbage_b64 = base64.b64encode(b"not a real png").decode()
        resp = _resp(json_body=_responses_body(b64=garbage_b64))
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="landscape", reference_characters=["grantley"]
            )
        assert result["success"] is False
        assert result["error_type"] == "invalid_response"

    def test_text_to_image_path_is_not_touched_by_the_backstop(self):
        """The reference-free path is unaffected by this fix — no
        measurement, no aspect_mismatch key, ever. Its 48 tests all assume
        `size` is honored as requested; this guards that assumption."""
        resp = _resp(json_body={"data": [{"b64_json": _b64_png_sized(962, 1634)}]})
        with patch(_POST, return_value=resp):
            result = CornnaImageGenProvider().generate(
                prompt="p", aspect_ratio="landscape"
            )
        assert result["success"] is True
        assert "aspect_mismatch" not in result
        assert "actual_size" not in result
