from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SubtitleRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    burn_in: bool = Field(
        False, description="If true, returns an MP4 with subtitles burned in. If false, returns a .srt file."
    )
    vertical_crop: bool = Field(
        False, description="Only used when burn_in=true: crop the video to 9:16 vertical (face-aware)."
    )


class ClipRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    start: str = Field(..., description="Start time — 'HH:MM:SS', 'MM:SS', or seconds")
    end: str = Field(..., description="End time — 'HH:MM:SS', 'MM:SS', or seconds")
    vertical_crop: bool = Field(True, description="Crop to 9:16 vertical using face detection")
    subtitles: Literal["none", "english", "native"] = Field(
        "none", description="Burn subtitles into the clip: none, english (translated), or native (as spoken)"
    )


class AnalyzeClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, le=20, description="Cap the number of clips. Omit to let the AI decide the count entirely on its own."
    )


class ViralClipInfo(BaseModel):
    title: Optional[str] = None
    start: str
    end: str
    duration_seconds: float
    score: Optional[float] = Field(default=None, ge=0, le=100)
    reason: Optional[str] = None
    hashtags: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class ViralClipsRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    max_clips: Optional[int] = Field(
        None, ge=1, le=20, description="Cap the number of clips. Omit to get every viral-worthy clip Gemini finds (typically 6-10)."
    )
    vertical_crop: bool = Field(True, description="Crop each clip to 9:16 vertical using face detection")
    subtitles: Literal["none", "english", "native"] = Field(
        "english", description="Burn subtitles into each clip: none, english (translated), or native (as spoken)"
    )
