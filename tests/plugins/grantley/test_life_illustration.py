"""Offline contract tests for Grantley's daily local illustration job."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from plugins.grantley import jobs
from plugins.grantley.jobs import run_life_advance, run_life_illustration
from plugins.grantley.store import GrantleyStore


@pytest.fixture
def store() -> GrantleyStore:
    return GrantleyStore(sqlite3.connect(":memory:", check_same_thread=False))


@pytest.fixture
def day() -> datetime:
    return datetime(2026, 8, 20, 0, 45, tzinfo=timezone.utc)


def _png(path: Path, color: tuple[int, int, int] = (91, 132, 178)) -> Path:
    Image.new("RGB", (20, 12), color).save(path, format="PNG")
    return path


class _FakeCornna:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _daily_beat(store: GrantleyStore, day: datetime, data_dir: Path) -> dict:
    result = run_life_advance(store, "grantley", when=day, data_dir=data_dir)
    assert result["ok"] is True
    return result


def _anchored_response(image: Path) -> dict:
    return {
        "success": True,
        "image": str(image),
        "provider": "cornna",
        "model": "gpt-image-2",
        "modality": "image",
        "characters": ["grantley"],
    }


def test_generates_valid_local_png_and_minimal_sidecar(
    store, day, tmp_path, monkeypatch
):
    advance = _daily_beat(store, day, tmp_path)
    source = _png(tmp_path / "provider.png")
    provider = _FakeCornna(_anchored_response(source))
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    result = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert result["ok"] is True
    assert result["skipped"] is False
    assert result["auto_beat_event_id"] > 0
    assert Path(result["image"]) == tmp_path / "life-images" / "grantley-2026-08-20.png"
    assert (
        Path(result["sidecar"]) == tmp_path / "life-images" / "grantley-2026-08-20.json"
    )
    with Image.open(result["image"]) as rendered:
        rendered.verify()

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["reference_characters"] == ["grantley"]
    assert call["aspect_ratio"] == "landscape"
    assert "Knights College" in call["prompt"]
    assert advance["current"]["activity"] in call["prompt"]

    sidecar = json.loads(Path(result["sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["date"] == "2026-08-20"
    assert sidecar["auto_beat_event_id"] == result["auto_beat_event_id"]
    assert sidecar["provider"] == "cornna"
    assert sidecar["model"] == "gpt-image-2"
    assert sidecar["image"] == "grantley-2026-08-20.png"
    assert sidecar["reference"] == "grantley"
    assert len(sidecar["current_life_sha256"]) == 64
    assert (
        sidecar["image_sha256"]
        == hashlib.sha256(Path(result["image"]).read_bytes()).hexdigest()
    )
    assert "prompt" not in sidecar
    assert "token" not in json.dumps(sidecar).lower()


def test_matching_daily_output_skips_without_a_second_provider_call(
    store, day, tmp_path, monkeypatch
):
    _daily_beat(store, day, tmp_path)
    provider = _FakeCornna(_anchored_response(_png(tmp_path / "provider.png")))
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    first = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )
    second = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert first["skipped"] is False
    assert second["ok"] is True
    assert second["skipped"] is True
    assert second["image"] == first["image"]
    assert len(provider.calls) == 1


def test_corrupt_same_day_image_is_safely_rebuilt(store, day, tmp_path, monkeypatch):
    _daily_beat(store, day, tmp_path)
    provider = _FakeCornna(_anchored_response(_png(tmp_path / "provider.png")))
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    first = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )
    Path(first["image"]).write_bytes(b"not an image")
    rebuilt = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert rebuilt["ok"] is True
    assert rebuilt["skipped"] is False
    assert len(provider.calls) == 2
    with Image.open(rebuilt["image"]) as rendered:
        rendered.verify()


def test_sidecar_replace_failure_rebuilds_inconsistent_pair(
    store, day, tmp_path, monkeypatch
):
    _daily_beat(store, day, tmp_path)
    old_source = _png(tmp_path / "old-provider.png", (91, 132, 178))
    new_source = _png(tmp_path / "new-provider.png", (178, 91, 132))
    final_source = _png(tmp_path / "final-provider.png", (132, 178, 91))
    provider = _FakeCornna(_anchored_response(old_source))
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    first = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )
    sidecar_path = Path(first["sidecar"])
    old_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    original_state = store.load_state("grantley")
    original_current = dict(original_state.state_json["life"]["current"])
    original_state.state_json["life"]["current"] = {
        **original_current,
        "activity": "a temporary illustration refresh",
    }
    store.upsert_state(original_state)

    original_replace = jobs.os.replace

    def fail_sidecar_replace(source, destination):
        if Path(destination).suffix == ".json":
            raise OSError("simulated sidecar replacement failure")
        original_replace(source, destination)

    provider.response = _anchored_response(new_source)
    monkeypatch.setattr(jobs.os, "replace", fail_sidecar_replace)
    failed = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert failed["ok"] is False
    assert failed["error"] == "storage_failed"
    assert (
        hashlib.sha256(Path(first["image"]).read_bytes()).hexdigest()
        != old_sidecar["image_sha256"]
    )
    assert json.loads(sidecar_path.read_text(encoding="utf-8")) == old_sidecar

    monkeypatch.setattr(jobs.os, "replace", original_replace)
    restored_state = store.load_state("grantley")
    restored_state.state_json["life"]["current"] = original_current
    store.upsert_state(restored_state)
    provider.response = _anchored_response(final_source)
    rebuilt = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert rebuilt["ok"] is True
    assert rebuilt["skipped"] is False
    assert len(provider.calls) == 3
    final_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert (
        final_sidecar["image_sha256"]
        == hashlib.sha256(Path(rebuilt["image"]).read_bytes()).hexdigest()
    )


def test_legacy_sidecar_without_image_digest_is_rebuilt(
    store, day, tmp_path, monkeypatch
):
    _daily_beat(store, day, tmp_path)
    provider = _FakeCornna(_anchored_response(_png(tmp_path / "provider.png")))
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    first = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )
    sidecar_path = Path(first["sidecar"])
    legacy_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    legacy_sidecar.pop("image_sha256")
    sidecar_path.write_text(json.dumps(legacy_sidecar), encoding="utf-8")

    rebuilt = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert rebuilt["ok"] is True
    assert rebuilt["skipped"] is False
    assert len(provider.calls) == 2
    assert "image_sha256" in json.loads(sidecar_path.read_text(encoding="utf-8"))


def test_missing_daily_beat_does_not_call_provider_or_write(
    store, day, tmp_path, monkeypatch
):
    provider = _FakeCornna(_anchored_response(_png(tmp_path / "provider.png")))
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    result = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert result["ok"] is False
    assert result["error"] == "life_beat_missing"
    assert provider.calls == []
    assert not (tmp_path / "life-images").exists()


@pytest.mark.parametrize(
    "asset_error", [FileNotFoundError("missing"), ValueError("corrupt")]
)
def test_unavailable_or_corrupt_reference_fails_closed(
    store, day, tmp_path, monkeypatch, asset_error
):
    _daily_beat(store, day, tmp_path)
    provider = _FakeCornna(_anchored_response(_png(tmp_path / "provider.png")))

    def fail_reference():
        raise asset_error

    monkeypatch.setattr(jobs, "_require_grantley_reference", fail_reference)
    result = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert result["ok"] is False
    assert result["error"] == "reference_unavailable"
    assert provider.calls == []
    assert not (tmp_path / "life-images").exists()


def test_provider_failure_leaves_no_partial_output(store, day, tmp_path, monkeypatch):
    _daily_beat(store, day, tmp_path)
    provider = _FakeCornna({
        "success": False,
        "error": "timed out",
        "error_type": "timeout",
    })
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    result = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert result["ok"] is False
    assert result["error"] == "provider_failed"
    assert result["provider_error_type"] == "timeout"
    assert not (tmp_path / "life-images").exists()


def test_storage_validation_failure_cleans_up_temporary_files(
    store, day, tmp_path, monkeypatch
):
    _daily_beat(store, day, tmp_path)
    provider = _FakeCornna(_anchored_response(_png(tmp_path / "provider.png")))
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)

    def reject_png(_raw: bytes) -> None:
        raise ValueError("simulated storage validation failure")

    monkeypatch.setattr(jobs, "_verify_stored_png", reject_png)
    result = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    output_dir = tmp_path / "life-images"
    assert result["ok"] is False
    assert result["error"] == "storage_failed"
    assert list(output_dir.glob(".*.tmp")) == []
    assert not (output_dir / "grantley-2026-08-20.png").exists()
    assert not (output_dir / "grantley-2026-08-20.json").exists()


def test_invalid_provider_image_and_unanchored_success_are_rejected(
    store, day, tmp_path, monkeypatch
):
    _daily_beat(store, day, tmp_path)
    monkeypatch.setattr(jobs, "_require_grantley_reference", lambda: None)
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not an image")
    provider = _FakeCornna(_anchored_response(invalid))

    result = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_image"
    assert not (tmp_path / "life-images").exists()

    provider.response = {
        **_anchored_response(_png(tmp_path / "provider.png")),
        "modality": "text",
        "reference_fallback": "missing_character_assets",
    }
    result = run_life_illustration(
        store, "grantley", when=day, data_dir=tmp_path, provider=provider
    )
    assert result["ok"] is False
    assert result["error"] == "reference_not_applied"
    assert not (tmp_path / "life-images").exists()


def test_cli_reports_missing_beat_as_structured_nonzero_json(tmp_path, capsys):
    script = (
        Path(__file__).resolve().parents[3]
        / "plugins"
        / "grantley"
        / "scripts"
        / "grantley_job.py"
    )
    module_spec = importlib.util.spec_from_file_location(
        "grantley_job_cli_test", script
    )
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    assert module.main(["--db", str(tmp_path / "data.db"), "illustrate"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "life_beat_missing"
