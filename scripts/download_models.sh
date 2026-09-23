#!/usr/bin/env bash
# Скачивает локальные модели (один раз, дальше работа без сети). ~0.7 ГБ.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models && cd models
# Диаризация: сегментация pyannote-3.0 (ONNX) + эмбеддинги NeMo TitaNet-small (sherpa-onnx)
if [ ! -f sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  curl -sSLO https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
  tar xjf sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 && rm sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
fi
[ -f nemo_en_titanet_small.onnx ] || curl -sSLO https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
cd ..
# ASR: GigaAM-Multilingual large CTC (ru + kk, MIT), int8 ONNX, + Silero VAD
.venv/bin/python - <<'PY'
import onnx_asr
onnx_asr.load_model("gigaam-multilingual-large-ctc", "models/gigaam-ml-large-ctc", quantization="int8")
onnx_asr.load_vad("silero", "models/silero-vad")
print("ASR models ready")
PY
