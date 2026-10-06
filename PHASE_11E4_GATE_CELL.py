# ═══════════════════════════════════════════════════════════════
# PHASE 11E — Voice Activation Gate (single Colab cell)
# Two-stage microphone activation gate for VERONICA
# Stage 1: OpenWakeWord ONNX streaming inference
# Stage 2: Independent audio-characteristic verifier
# ═══════════════════════════════════════════════════════════════

import numpy as np
import sounddevice as sd
import webrtcvad
from openwakeword import Model

# ── CONFIG ──────────────────────────────────────────────────────
WAKE_MODEL_PATH = "/content/veronica_training/veronica_wake_model.onnx"
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280          # 80 ms — openWakeWord native chunk
CAPTURE_SECONDS = 1           # audio window captured on wake candidate
STAGE1_THRESHOLD = 0.5        # openWakeWord score floor (stages 1+2 both required)
STAGE2_VAD_THRESHOLD = 3      # webrtcvad aggressiveness (0-3)
MFCC_SIMILARITY_FLOOR = 0.65  # minimum cosine similarity to Veronica template

# ── STAGE 2: INDEPENDENT VERIFIER ──────────────────────────────
class AudioCharacteristicVerifier:
    """Independent second stage — verifies raw audio characteristics,
    NOT the openWakeWord model's internal scoring."""

    def __init__(self, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.vad = webrtcvad.Vad(STAGE2_VAD_THRESHOLD)
        self.reference_mfcc = None  # set via record_reference()

    def record_reference(self, duration=2.0):
        """Record a 'Veronica' template for MFCC comparison.
        Call once before activation gate."""
        print(f"Recording Veronica reference ({duration}s)... Speak clearly: 'Veronica'")
        ref_audio = sd.rec(int(duration * self.sample_rate),
                           samplerate=self.sample_rate, dtype='int16', channels=1)
        sd.wait()
        self.reference_mfcc = self._compute_mfcc(ref_audio.flatten())
        print("Reference recorded.")

    def _compute_mfcc(self, audio_int16):
        """Compute MFCCs using soundfile + scipy (no tensorflow/tflite)."""
        from scipy.fftpack import dct
        import scipy.signal as signal

        audio = audio_int16.astype(np.float64) / 32768.0
        # Pre-emphasis
        emphasized = np.append(audio[0], audio[1:] - 0.97 * audio[:-1])
        # Framing (25ms window, 10ms hop)
        frame_len = int(0.025 * self.sample_rate)
        hop_len = int(0.010 * self.sample_rate)
        frames = []
        for i in range(0, len(emphasized) - frame_len, hop_len):
            frames.append(emphasized[i:i + frame_len])
        if not frames:
            return np.zeros(13)
        frames = np.stack(frames)
        # Window + FFT + mel filterbank
        windowed = frames * np.hanning(frame_len)[:, None]
        spec = np.abs(np.fft.rfft(windowed, axis=1))
        # Mel filterbank (40 filters)
        n_mels = 40
        low_mel = 0
        high_mel = 2595 * np.log10(1 + (self.sample_rate / 2) / 700)
        mel_pts = np.linspace(low_mel, high_mel, n_mels + 2)
        hz_pts = 700 * (10 ** (mel_pts / 2595) - 1)
        bin_pts = np.floor((spec.shape[1] + 1) * hz_pts / self.sample_rate).astype(int)
        fbank = np.zeros((n_mels, spec.shape[1]))
        for m in range(1, n_mels + 1):
            f_left, f_center, f_right = bin_pts[m - 1], bin_pts[m], bin_pts[m + 1]
            for k in range(f_left, f_center):
                fbank[m - 1, k] = (k - bin_pts[m - 1]) / (bin_pts[m] - bin_pts[m - 1])
            for k in range(f_center, f_right):
                fbank[m - 1, k] = (bin_pts[m + 1] - k) / (bin_pts[m + 1] - bin_pts[m])
        mel = np.dot(fbank, spec.T).T
        mel = np.where(mel == 0, np.finfo(float).eps, mel)
        mel = np.log(mel)
        # DCT → 13 MFCCs
        mfcc = dct(mel, type=2, axis=1, norm='ortho')[:, :13]
        return mfcc.mean(axis=0)  # aggregate to single vector

    def _vad_ratio(self, audio_int16):
        """Fraction of frames classified as speech by webrtcvad."""
        audio = audio_int16.copy()
        if audio.dtype != np.int16:
            audio = (audio * 32767).astype(np.int16)
        frame_bytes = (self.sample_rate * 30 // 1000) * 2  # 30ms frames
        n_frames = (len(audio) * 2 - frame_bytes) // (frame_bytes // 2) + 1
        if n_frames < 1:
            return 0.0
        speech_frames = 0
        for i in range(n_frames):
            start = i * (frame_bytes // 2)
            frame = audio[start:start + frame_bytes // 2].tobytes()
            if len(frame) == frame_bytes and self.vad.is_speech(frame, self.sample_rate):
                speech_frames += 1
        return speech_frames / n_frames

    def verify(self, audio_int16):
        """Return True if audio passes independent verification.
        Checks: VAD speech present + MFCC similarity to reference."""
        # Check 1: VAD must detect speech
        vad_ratio = self._vad_ratio(audio_int16)
        if vad_ratio < 0.3:
            return False, f"VAD speech ratio {vad_ratio:.2f} < 0.3"

        # Check 2: MFCC similarity to Veronica reference
        if self.reference_mfcc is None:
            return True, "no reference — VAD pass only"
        candidate_mfcc = self._compute_mfcc(audio_int16)
        similarity = np.dot(candidate_mfcc, self.reference_mfcc) / (
            np.linalg.norm(candidate_mfcc) * np.linalg.norm(self.reference_mfcc) + 1e-8
        )
        if similarity < MFCC_SIMILARITY_FLOOR:
            return False, f"MFCC similarity {similarity:.3f} < {MFCC_SIMILARITY_FLOOR}"

        return True, f"VAD {vad_ratio:.2f} + MFCC sim {similarity:.3f}"


# ── STAGE 1: LOAD WAKE MODEL ───────────────────────────────────
print("Loading wake model (ONNX, streaming)...")
wake_model = Model(
    wakeword_models=[WAKE_MODEL_PATH],
    inference_framework='onnx',
)
model_name = list(wake_model.models.keys())[0]
print(f"Model loaded: {model_name}")

# ── STAGE 2: INIT VERIFIER ─────────────────────────────────────
verifier = AudioCharacteristicVerifier()
verifier.record_reference(duration=2.0)

# ── STREAMING GATE ─────────────────────────────────────────────
print("\nListening... (Ctrl+C to stop)\n")
buffer = np.array([], dtype=np.int16)
wake_detected = False

try:
    def audio_callback(indata, frames, time_info, status):
        """Called every CHUNK_SAMPLES (~80ms) from microphone."""
        nonlocal buffer, wake_detected
        if status:
            print(f"Audio status: {status}")
        chunk = indata[:, 0].astype(np.int16)
        buffer = np.concatenate([buffer, chunk])

        # Stage 1: openWakeWord streaming predict (chunk = 1280 ints @ 16kHz)
        scores = wake_model.predict(chunk)
        score = scores.get(model_name, 0.0)

        if score >= STAGE1_THRESHOLD and not wake_detected:
            wake_detected = True
            print(f"\nWake candidate detected (score={score:.3f}) — Verifying...")

            # Capture 1s window ending at this chunk
            keep = int(CAPTURE_SECONDS * SAMPLE_RATE)
            if len(buffer) >= keep:
                window = buffer[-keep:]
            else:
                window = buffer.copy()

            # Stage 2: independent audio characteristic verification
            passed, detail = verifier.verify(window)
            if passed:
                print(f"VERIFIED: VERONICA ACTIVATED ({detail})")
            else:
                print(f"Rejected ({detail})")
            wake_detected = False
        elif score > 0.05:
            print(f"  Listening... score={score:.3f}", end="\r")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=CHUNK_SAMPLES,
        dtype='int16',
        channels=1,
        callback=audio_callback,
    ):
        while True:
            sd.sleep(1000)

except KeyboardInterrupt:
    print("\nStopped.")
