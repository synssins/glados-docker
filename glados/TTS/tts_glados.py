"""Local GLaDOS TTS — ported from dnhkng/GLaDOS for self-contained inference.

VITS ONNX model + local phonemizer. Zero external services, zero espeak,
zero HuggingFace. All model files live under `GLADOS_TTS_MODELS_DIR`
(default `/app/models/TTS`).
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
from pickle import load
from typing import Any

import numpy as np
from numpy.typing import NDArray
import onnxruntime as ort  # type: ignore

from .phonemizer import Phonemizer

ort.set_default_logger_severity(4)


def _models_dir() -> Path:
    return Path(os.environ.get("GLADOS_TTS_MODELS_DIR", "/app/models/TTS"))


@dataclass
class PiperConfig:
    num_symbols: int
    num_speakers: int
    sample_rate: int
    espeak_voice: str
    length_scale: float
    noise_scale: float
    noise_w: float
    phoneme_id_map: Mapping[str, Sequence[int]]
    speaker_id_map: dict[str, int] | None = None

    @staticmethod
    def from_dict(config: dict[str, Any]) -> "PiperConfig":
        inference = config.get("inference", {})
        return PiperConfig(
            num_symbols=config["num_symbols"],
            num_speakers=config["num_speakers"],
            sample_rate=config["audio"]["sample_rate"],
            noise_scale=inference.get("noise_scale", 0.667),
            length_scale=inference.get("length_scale", 1.0),
            noise_w=inference.get("noise_w", 0.8),
            espeak_voice=config["espeak"]["voice"],
            phoneme_id_map=config["phoneme_id_map"],
            speaker_id_map=config.get("speaker_id_map", {}),
        )


class SpeechSynthesizer:
    """Text → 22050 Hz float32 audio via VITS ONNX. Self-contained."""

    MAX_WAV_VALUE = 32767.0

    PAD = "_"
    BOS = "^"
    EOS = "$"

    def __init__(
        self,
        model_path: Path | None = None,
        phoneme_path: Path | None = None,
        speaker_id: int | None = None,
    ) -> None:
        base = _models_dir()
        if model_path is None:
            model_path = base / "glados.onnx"
        if phoneme_path is None:
            phoneme_path = base / "phoneme_to_id.pkl"

        providers = ort.get_available_providers()
        if "TensorrtExecutionProvider" in providers:
            providers.remove("TensorrtExecutionProvider")
        if "CoreMLExecutionProvider" in providers:
            providers.remove("CoreMLExecutionProvider")

        self.ort_sess = ort.InferenceSession(
            str(model_path),
            sess_options=ort.SessionOptions(),
            providers=providers,
        )
        self.phonemizer = Phonemizer()
        self.id_map = self._load_pickle(phoneme_path)

        # Config lives alongside the .onnx, either as <stem>.json or <stem>.onnx.json
        config_path = model_path.with_suffix(".json")
        if not config_path.exists():
            alt = model_path.parent / (model_path.name + ".json")
            if alt.exists():
                config_path = alt
        try:
            with open(config_path, encoding="utf-8") as f:
                config_dict = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Voice config not found: {config_path}") from None
        self.config = PiperConfig.from_dict(config_dict)
        self.sample_rate = self.config.sample_rate
        self.speaker_id = (
            self.config.speaker_id_map.get(str(speaker_id), 0)
            if self.config.num_speakers > 1 and self.config.speaker_id_map is not None
            else None
        )

    @staticmethod
    def _load_pickle(path: Path) -> dict[str, Any]:
        with path.open("rb") as f:
            return dict(load(f))

    def generate_speech_audio(
        self,
        text: str,
        length_scale: float | None = None,
        noise_scale: float | None = None,
        noise_w: float | None = None,
    ) -> NDArray[np.float32]:
        phonemes = self._phonemizer(text)
        phoneme_ids_list = [self._phonemes_to_ids(sentence) for sentence in phonemes]
        synth_kwargs: dict[str, float] = {}
        if length_scale is not None:
            synth_kwargs["length_scale"] = length_scale
        if noise_scale is not None:
            synth_kwargs["noise_scale"] = noise_scale
        if noise_w is not None:
            synth_kwargs["noise_w"] = noise_w
        audio_chunks = [self._synthesize_ids_to_audio(phoneme_ids, **synth_kwargs) for phoneme_ids in phoneme_ids_list]

        if audio_chunks:
            audio: NDArray[np.float32] = np.concatenate(audio_chunks, axis=1).T
            return audio
        return np.array([], dtype=np.float32)

    def _phonemizer(self, input_text: str) -> list[str]:
        return self.phonemizer.convert_to_phonemes([input_text], "en_us")

    def _phonemes_to_ids(self, phonemes: str) -> list[int]:
        ids: list[int] = list(self.id_map[self.BOS])
        for phoneme in phonemes:
            if phoneme not in self.id_map:
                continue
            ids.extend(self.id_map[phoneme])
            ids.extend(self.id_map[self.PAD])
        ids.extend(self.id_map[self.EOS])
        return ids

    def _synthesize_ids_to_audio(
        self,
        phoneme_ids: list[int],
        length_scale: float | None = None,
        noise_scale: float | None = None,
        noise_w: float | None = None,
    ) -> NDArray[np.float32]:
        if length_scale is None:
            length_scale = self.config.length_scale
        if noise_scale is None:
            noise_scale = self.config.noise_scale
        if noise_w is None:
            noise_w = self.config.noise_w

        phoneme_ids_array = np.expand_dims(np.array(phoneme_ids, dtype=np.int64), 0)
        phoneme_ids_lengths = np.array([phoneme_ids_array.shape[1]], dtype=np.int64)
        scales = np.array([noise_scale, length_scale, noise_w], dtype=np.float32)

        sid = None
        if self.speaker_id is not None:
            sid = np.array([self.speaker_id], dtype=np.int64)

        audio: NDArray[np.float32] = self.ort_sess.run(
            None,
            {
                "input": phoneme_ids_array,
                "input_lengths": phoneme_ids_lengths,
                "scales": scales,
                "sid": sid,
            },
        )[0].squeeze((0, 1))

        return audio

    def __del__(self) -> None:
        if hasattr(self, "ort_sess"):
            del self.ort_sess
