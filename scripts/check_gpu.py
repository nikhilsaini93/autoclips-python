#!/usr/bin/env python3
"""GPU readiness check for Whisper (faster-whisper + CTranslate2 + PyAV).

Run on the machine that will transcribe:

    python scripts/check_gpu.py           # libs + CUDA device count + decode test
    python scripts/check_gpu.py --full    # also transcribes 1s of silence (tiny model)

Exit code 0 means the CUDA transcription path is healthy. Catches the two
failures seen on Colab after it moved to the CUDA 13 toolkit:
  - RuntimeError: Library libcublas.so.12 is not found (CTranslate2 needs the
    CUDA 12 runtime shipped in the nvidia pip wheels)
  - TypeError: open() got an unexpected keyword argument 'metadata_errors'
    (PyAV >= 19 vs faster-whisper 1.2.1; requirements.txt pins av==18.1.0)
"""

import argparse
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.video.transcribe import (  # noqa: E402
    ensure_faster_whisper_av_compat,
    preload_ctranslate2_cuda_libs,
)


def _write_silence_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Whisper GPU readiness check")
    parser.add_argument(
        "--full",
        action="store_true",
        help="also run a tiny GPU transcription (downloads the tiny model once)",
    )
    args = parser.parse_args()
    failures: list[str] = []

    preloaded = preload_ctranslate2_cuda_libs()
    if preloaded:
        print(f"[ok] preloaded CUDA 12 libs for CTranslate2 ({len(preloaded)}):")
        for p in preloaded:
            print(f"     - {p}")
    else:
        print("[--] no CUDA libs preloaded (Windows/macOS, CPU host, or already loadable)")

    try:
        import av

        av_major = int(av.__version__.split(".")[0])
        print(f"[ok] PyAV {av.__version__}")
    except Exception as e:
        print(f"[!!] PyAV import failed: {type(e).__name__}: {e}")
        return 1

    try:
        import faster_whisper

        print(f"[ok] faster-whisper {faster_whisper.__version__}")
    except Exception as e:
        print(f"[!!] faster-whisper import failed: {type(e).__name__}: {e}")
        return 1

    shimmed = ensure_faster_whisper_av_compat()
    if shimmed:
        print("[ok] PyAV >= 19 detected - decode_audio compatibility shim installed")
    elif av_major >= 19:
        print("[!!] PyAV >= 19 but the compatibility shim was not installed")
        failures.append("pyav shim")

    import ctranslate2

    print(f"[ok] CTranslate2 {ctranslate2.__version__}")
    try:
        devices = ctranslate2.get_cuda_device_count()
    except Exception as e:
        print(f"[!!] CTranslate2 could not query CUDA devices: {type(e).__name__}: {e}")
        devices = 0
    if devices < 1:
        print("[!!] CTranslate2 sees 0 CUDA devices - no GPU runtime? "
              "(Colab: Runtime > Change runtime type > T4 GPU, then restart)")
        failures.append("cuda devices")
    else:
        print(f"[ok] CUDA devices visible to CTranslate2: {devices}")

    with tempfile.TemporaryDirectory() as tmp:
        wav = _write_silence_wav(Path(tmp) / "silence.wav")

        # Real decode test: catches the metadata_errors TypeError exactly where
        # faster-whisper hits it (transcribe() -> decode_audio()).
        try:
            from faster_whisper.audio import decode_audio

            audio = decode_audio(str(wav))
            print(f"[ok] audio decode smoke test ({audio.shape[0]} samples, {audio.dtype})")
        except Exception as e:
            print(f"[!!] audio decode failed: {type(e).__name__}: {e}")
            failures.append("audio decode")

        if args.full:
            if devices < 1:
                print("[--] skipping --full transcription (no CUDA devices)")
            else:
                try:
                    from faster_whisper import WhisperModel

                    model = WhisperModel("tiny", device="cuda", compute_type="float16")
                    segments, _ = model.transcribe(str(wav))
                    count = sum(1 for _ in segments)
                    print(f"[ok] tiny CUDA transcription ran ({count} segments)")
                except Exception as e:
                    print(f"[!!] CUDA transcription failed: {type(e).__name__}: {e}")
                    failures.append("cuda transcribe")

    if failures:
        print(f"\nFAIL: {'; '.join(failures)}")
        return 1
    print("\nPASS: Whisper CUDA path is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
