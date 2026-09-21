"""Convenience re-exports — lazy so importing `app.services.video` does not
require heavy optional deps (cv2 / faster-whisper) unless actually used."""

_LAZY = {
    "cleanup_old_files": "app.services.video.cleanup",
    "download_video": "app.services.video.download",
    "get_video_credit": "app.services.video.download",
    "get_video_dimensions": "app.services.video.download",
    "get_video_duration": "app.services.video.download",
    "detect_face_center_x": "app.services.video.faces",
    "detect_face_center_x_in_frame": "app.services.video.faces",
    "find_viral_clips": "app.services.video.gemini",
    "get_genai_client": "app.services.video.gemini",
    "get_video_id": "app.services.video.ids",
    "run": "app.services.video.process",
    "render_video": "app.services.video.render",
    "chunk_words_for_captions": "app.services.video.subtitles",
    "format_srt_timestamp": "app.services.video.subtitles",
    "write_srt": "app.services.video.subtitles",
    "time_to_seconds": "app.services.video.timeutils",
    "get_whisper_model": "app.services.video.transcribe",
    "transcribe_words_english": "app.services.video.transcribe",
    "transcribe_words_native": "app.services.video.transcribe",
    "words_to_transcript_text": "app.services.video.transcribe",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        mod = importlib.import_module(_LAZY[name])
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
