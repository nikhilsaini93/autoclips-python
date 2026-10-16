"""Tests for the CUDA/PyAV runtime compatibility helpers in
app/services/video/transcribe.py (used by scripts/check_gpu.py):

- preload_ctranslate2_cuda_libs() fixes `Library libcublas.so.12 is not found`
  on hosts (Colab) whose toolkit is CUDA 13 while CTranslate2 needs CUDA 12.
- ensure_faster_whisper_av_compat() fixes the PyAV >= 19 `metadata_errors`
  TypeError when requirements.txt's av<19 pin is bypassed.
"""

import sys
import types
from pathlib import Path

import pytest

from app.services.video import transcribe


@pytest.fixture(autouse=True)
def _reset_compat_flags(monkeypatch):
    monkeypatch.setattr(transcribe, "_cuda_preload_done", False)
    monkeypatch.setattr(transcribe, "_pyav_compat_done", False)


def test_preload_noop_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        transcribe, "_cuda_lib_search_dirs", lambda: pytest.fail("must not search")
    )
    assert transcribe.preload_ctranslate2_cuda_libs() == []


def test_preload_dlopens_found_libs_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    for name in ("libcudart.so.12", "libcublasLt.so.12", "libcublas.so.12"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(transcribe, "_cuda_lib_search_dirs", lambda: [tmp_path])
    calls = []
    monkeypatch.setattr(transcribe.ctypes, "CDLL", lambda path, **kw: calls.append((path, kw)))

    loaded = transcribe.preload_ctranslate2_cuda_libs()

    assert [Path(p).name for p in loaded] == [
        "libcudart.so.12",
        "libcublasLt.so.12",
        "libcublas.so.12",
    ]
    assert len(calls) == 3
    assert calls[0][1]["mode"] == transcribe.ctypes.RTLD_GLOBAL

    assert transcribe.preload_ctranslate2_cuda_libs() == []
    assert len(calls) == 3


def test_preload_survives_load_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "libcublas.so.12").write_bytes(b"")
    monkeypatch.setattr(transcribe, "_cuda_lib_search_dirs", lambda: [tmp_path])

    def boom(path, **kw):
        raise OSError("libcublasLt.so.12: cannot open shared object file")

    monkeypatch.setattr(transcribe.ctypes, "CDLL", boom)
    assert transcribe.preload_ctranslate2_cuda_libs() == []


def test_find_cuda_lib_versioned_fallback(tmp_path):
    (tmp_path / "libcudnn.so.9.5.0").write_bytes(b"")
    assert transcribe._find_cuda_lib(tmp_path, "libcudnn.so.9").name == "libcudnn.so.9.5.0"
    assert transcribe._find_cuda_lib(tmp_path, "libcublas.so.12") is None


def test_av_compat_noop_when_supported(monkeypatch):
    monkeypatch.setattr(transcribe, "_pyav_supports_metadata_errors", lambda: True)
    assert transcribe.ensure_faster_whisper_av_compat() is False


def test_av_compat_rebinds_decode_audio(monkeypatch):
    fake_audio = types.ModuleType("faster_whisper.audio")
    fake_transcribe = types.ModuleType("faster_whisper.transcribe")
    fake_pkg = types.ModuleType("faster_whisper")
    fake_pkg.audio = fake_audio
    fake_pkg.transcribe = fake_transcribe
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_pkg)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", fake_audio)
    monkeypatch.setitem(sys.modules, "faster_whisper.transcribe", fake_transcribe)
    monkeypatch.setattr(transcribe, "_pyav_supports_metadata_errors", lambda: False)

    assert transcribe.ensure_faster_whisper_av_compat() is True
    assert fake_audio.decode_audio is transcribe._decode_audio_av19
    assert fake_transcribe.decode_audio is transcribe._decode_audio_av19
    assert transcribe.ensure_faster_whisper_av_compat() is False


def test_decode_audio_smoke(tmp_path):
    pytest.importorskip("av")
    pytest.importorskip("faster_whisper")
    import wave

    wav = tmp_path / "silence.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)

    transcribe.ensure_faster_whisper_av_compat()
    from faster_whisper.audio import decode_audio

    audio = decode_audio(str(wav))
    assert str(audio.dtype) == "float32"
    assert 1500 <= len(audio) <= 1700
