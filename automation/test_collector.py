#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.collector import (
    extract_fulltext,
    normalize_candidate,
    score_candidate,
    safe_key,
    run,
    parse_args,
)


BASE = Path(__file__).parent


class CollectorTests(unittest.TestCase):
    def test_title_only_keys_are_stable_and_distinct(self):
        self.assertEqual(safe_key({"title": "First paper"}), "title:first paper")
        self.assertNotEqual(safe_key({"title": "First paper"}), safe_key({"title": "Second paper"}))

    def test_relevance_requires_signal_and_preserves_metadata(self):
        candidate = normalize_candidate({
            "title": "Long-read metagenomics reveals age-associated gut microbial genomes",
            "authorString": "A. Example, B. Example",
            "journalTitle": "Example Aging Journal",
            "pubYear": "2026",
            "doi": "10.0000/example.1",
            "pmcid": "PMC1234567",
            "abstractText": "Ageing and senescence were studied with PacBio long-read sequencing and multi-omics.",
        })
        score, evidence = score_candidate(candidate)
        self.assertGreaterEqual(score, 12)
        self.assertEqual(candidate["authors"], ["A. Example", "B. Example"])
        self.assertIn("aging", " ".join(evidence))

    def test_xml_extracts_original_figure_and_sections(self):
        xml = (BASE / "fixtures/pmc-example.xml").read_text(encoding="utf-8")
        fulltext, methods, results, figures, supplements = extract_fulltext(xml, "PMC1234567")
        self.assertIn("PacBio HiFi", methods)
        self.assertIn("complete contigs", results)
        self.assertIn("Age-associated microbial genomes", fulltext)
        self.assertEqual(figures[0]["label"], "Fig. 1")
        self.assertTrue(figures[0]["image_url"].endswith("/example-f1.jpg"))
        self.assertIn("Workflow", figures[0]["caption"])
        self.assertEqual(supplements[0]["label"], "Supplementary Table 1")
        self.assertTrue(supplements[0]["url"].endswith("/example-supplement.pdf"))

    def test_dry_run_does_not_publish_without_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = parse_args([
                "--fixture", str(BASE / "fixtures/europepmc.json"),
                "--xml-fixture", str(BASE / "fixtures/pmc-example.xml"),
                "--state", str(root / "state.json"),
                "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"),
                "--max-new", "5",
            ])
            with patch.dict(os.environ, {"EASYSCHOLAR_SECRET_KEY": ""}, clear=False):
                summary = run(args)
            self.assertEqual(summary["selected"], 1)
            self.assertEqual(summary["published"], 0)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(output["articles"], [])
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["processed"][0]["status"], "manual_review")
            self.assertIn("missing_llm_key", report["processed"][0]["ai_status"])

    def test_structured_llm_draft_passes_gate_and_is_written(self):
        article = {
            "title_zh": "长读长宏基因组揭示年龄相关肠道基因组",
            "lead": "这是一段来源可追溯的导语。",
            "background": "背景段落。",
            "research_question": "研究问题。",
            "study_design": "研究设计。",
            "methods": "完整方法翻译。",
            "results": "按正文翻译的结果。",
            "limitations": "局限。",
            "takeaways": "启示。",
            "figure_notes": [{"label": "图1", "caption_zh": "图注翻译", "interpretation": "读图说明"}],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = parse_args([
                "--fixture", str(BASE / "fixtures/europepmc.json"),
                "--xml-fixture", str(BASE / "fixtures/pmc-example.xml"),
                "--state", str(root / "state.json"),
                "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"),
            ])
            with patch("automation.collector.generate_llm_article", return_value=(article, "success")):
                summary = run(args)
            self.assertEqual(summary["published"], 1)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(output["articles"][0]["authors"], ["A. Example", "B. Example"])
            self.assertIn("Example Aging Journal", output["articles"][0]["journal_display"])
            self.assertEqual(output["articles"][0]["status"], "published")
            self.assertNotIn("fulltext_text", output["articles"][0])
            self.assertNotIn("methods_text", output["articles"][0])
            self.assertEqual(output["articles"][0]["supplements"][0]["label"], "Supplementary Table 1")

    def test_second_run_preserves_published_record_and_does_not_duplicate(self):
        article = {
            "title_zh": "长读长宏基因组揭示年龄相关肠道基因组",
            "lead": "导语", "background": "背景", "research_question": "问题",
            "study_design": "设计", "methods": "方法", "results": "结果",
            "limitations": "局限", "takeaways": "启示",
            "figure_notes": [{"label": "图1", "caption_zh": "图注", "interpretation": "解读"}],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = parse_args([
                "--fixture", str(BASE / "fixtures/europepmc.json"),
                "--xml-fixture", str(BASE / "fixtures/pmc-example.xml"),
                "--state", str(root / "state.json"),
                "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"),
            ])
            with patch("automation.collector.generate_llm_article", return_value=(article, "success")):
                self.assertEqual(run(args)["published"], 1)
                self.assertEqual(run(args)["selected"], 0)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(len(output["articles"]), 1)


if __name__ == "__main__":
    unittest.main()
