import io
import struct
import warnings
import wave

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop
from app.core.config import settings

def calculate_rms(chunk: bytes) -> float:
    """Calculates Root Mean Square volume of a PCM 16-bit audio chunk."""
    if not chunk or len(chunk) < 2:
        return 0.0
    count = len(chunk) // 2
    try:
        shorts = struct.unpack(f"<{count}h", chunk[:count * 2])
        sum_squares = sum(s * s for s in shorts)
        return (sum_squares / count) ** 0.5
    except (struct.error, ValueError, OverflowError):
        return 0.0

def resample_pcm_16k(pcm_bytes: bytes, in_rate: int, sample_width: int = 2, channels: int = 1) -> tuple[bytes, int]:
    """Downsamples PCM audio (e.g., 24kHz to 16kHz) for efficient STT uploads."""
    target_rate = settings.stt_sample_rate
    if in_rate == target_rate:
        return pcm_bytes, target_rate
    try:
        converted, _ = audioop.ratecv(pcm_bytes, sample_width, channels, in_rate, target_rate, None)
        return converted, target_rate
    except (audioop.error, struct.error, ValueError, OverflowError):
        return pcm_bytes, in_rate

def pcm_to_wav(pcm_bytes: bytes, sample_rate: int, sample_width: int = 2, channels: int = 1) -> bytes:
    """Encodes raw PCM bytes into a standard in-memory WAV container."""
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return wav_buf.getvalue()
