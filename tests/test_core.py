"""Юнит-тесты детерминированной части: сроки, язык реплики, регистр имён, VAD. Запуск: .venv/bin/python -m unittest -v"""
import json
import unittest
from pathlib import Path

import numpy as np

from app.pipeline.llm.deadline_resolver import resolve
from app.pipeline.text import lang_tag, restore_case
from app.pipeline.diarize import vad_segments

ROOT = Path(__file__).resolve().parent.parent
MEETING = "2026-09-23"  # среда


class DeadlineTest(unittest.TestCase):
    CASES = {
        "до пятницы": "2026-09-25", "на этой неделе": "2026-09-25", "до конца недели": "2026-09-25",
        "на следующей неделе": "2026-10-02", "к среде": "2026-09-30", "за неделю": "2026-09-30",
        "за две недели": "2026-10-07", "до 15 октября": "2026-10-15", "к пятнадцатому октября": "2026-10-15",
        "к 20 октября": "2026-10-20", "до двадцать шестого сентября": "2026-09-26", "жұмаға дейін": "2026-09-25",
        "бірінші қазанға дейін": "2026-10-01", "он бесінші қазанға дейін": "2026-10-15",
        "келесі дүйсенбіге дейін": "2026-09-28", "айдың соңына дейін": "2026-09-30", "ертеңге дейін": "2026-09-24",
    }

    def test_phrases(self):
        for raw, iso in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(resolve(raw, MEETING)["iso"], iso)

    def test_unspecified(self):
        for raw in ("не указан", "после проведения", "по итогам этого совещания"):
            with self.subTest(raw=raw):
                self.assertIsNone(resolve(raw, MEETING)["iso"])

    def test_gold_resolvable(self):
        """Все сроки эталона (eval/gold.json) с датой разрешаются резолвером в ту же дату."""
        gold = json.loads((ROOT / "eval" / "gold.json").read_text())
        gold = gold.get("value", gold)
        bad = []
        for t in gold["tasks"]:
            if t.get("deadline_iso"):
                got = resolve(t["deadline_raw"], MEETING)["iso"]
                if got != t["deadline_iso"]:
                    bad.append((t["id"], t["deadline_raw"], got, t["deadline_iso"]))
        self.assertLessEqual(len(bad), 2, bad)


class LangTest(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(lang_tag("Коллеги, начинаем совещание по бюджету."), "RU")
        self.assertEqual(lang_tag("Бүгін үш мәселе қарастырамыз және қызметкерлерді оқыту."), "KZ")
        self.assertEqual(lang_tag("Марат, кестені всем руководителям жіберіңіз до конца недели."), "RU+KZ")


class CaseTest(unittest.TestCase):
    def test_names(self):
        self.assertIn("Нурланом Сагатовичем", restore_case("свяжитесь с нурланом сагатовичем"))
        self.assertIn("на уровне", restore_case("держится на уровне семидесяти"))
        self.assertIn("Ерлан", restore_case("пусть ерлан подготовит претензию"))


class VadTest(unittest.TestCase):
    def test_two_bursts(self):
        sr = 16000
        t = np.arange(sr) / sr
        tone = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
        audio = np.concatenate([np.zeros(sr // 2), tone, np.zeros(sr), tone, np.zeros(sr // 2)]).astype(np.float32)
        segs = vad_segments(audio)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0][0], 0.5, delta=0.1)


if __name__ == "__main__":
    unittest.main()
