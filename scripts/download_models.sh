#!/usr/bin/env bash
# Скачивает локальные модели распознавания и диаризации (один раз, дальше работа без сети), ~0.63 ГБ.
# То же самое умеет «Мастер настройки» в веб-интерфейсе (app/setup.py).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models
# Голосовые эмбеддинги для диаризации: NeMo TitaNet-small (ONNX из релизов sherpa-onnx), 38 МБ
[ -f models/nemo_en_titanet_small.onnx ] || curl -sSL -o models/nemo_en_titanet_small.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
# ASR: GigaAM-Multilingual large CTC (ru + kk, MIT), int8 ONNX, ~592 МБ; детектор речи Silero VAD, 2 МБ
.venv/bin/python - <<'PY'
import onnx_asr
onnx_asr.load_model("gigaam-multilingual-large-ctc", "models/gigaam-ml-large-ctc", quantization="int8")
onnx_asr.load_vad("silero", "models/silero-vad")
print("Модели готовы: models/")
PY
