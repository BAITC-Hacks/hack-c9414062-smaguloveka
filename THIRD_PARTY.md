# Сторонние компоненты

| Компонент | Где используется | Лицензия |
|---|---|---|
| GigaAM-Multilingual large CTC (ai-sage), ONNX-экспорт istupakov/gigaam-multilingual-large-ctc-onnx | распознавание речи RU/KZ | MIT |
| onnx-asr | загрузка и запуск ASR | MIT |
| sherpa-onnx (k2-fsa) | извлечение голосовых эмбеддингов | Apache-2.0 |
| NeMo TitaNet-small (NVIDIA), ONNX из релизов sherpa-onnx | голосовые эмбеддинги для диаризации | см. карточку модели NVIDIA NeMo (CC-BY-4.0) |
| Silero VAD | загружается `download_models.sh` (в текущем пайплайне не используется) | MIT |
| gemma4 (Google), через Ollama | извлечение поручений и саммари | Apache-2.0 (по данным `ollama show gemma4`) |
| React 18 (UMD) | интерфейс | MIT |
| IBM Plex Sans / Mono | шрифты интерфейса | SIL OFL 1.1 |
| Montserrat | шрифт PDF-экспорта (`app/static/fonts/pdf/OFL.txt`) | SIL OFL 1.1 |
| FastAPI, uvicorn, python-docx, fpdf2, numpy, soundfile | backend | MIT / BSD / LGPL (fpdf2) |
| Логика `speaker_names`, `deadline_resolver`, `verifier`, `extract` | написана в ходе хакатона | — |
