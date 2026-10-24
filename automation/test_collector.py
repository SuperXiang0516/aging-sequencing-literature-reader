#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
import urllib.error
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

    def test_run_skips_doi_only_hits_and_continues_after_failed_candidate(self):
        def candidate(identifier, pmcid):
            return {
                "id": identifier,
                "title": identifier,
                "authors": [],
                "journal": "Example Journal",
                "year": "2026",
                "doi": identifier,
                "pmcid": pmcid,
                "pmid": "",
                "abstract": "ageing sequencing",
                "fulltext_urls": [],
                "source": "fixture",
                "relevance_score": 20,
                "relevance_evidence": ["aging: aging"],
            }

        failed = candidate("10.0000/failed", "PMCFAIL")
        successful = candidate("10.0000/success", "PMCSUCCESS")
        doi_only = candidate("10.0000/doi-only", "")
        failed_record = dict(failed, fulltext_ok=True, figures=[{"label": "Fig. 1"}],
                             article={}, ai_status="error", publishable=False,
                             gate_reasons=["AI 字段缺失：results"], status="manual_review",
                             updated_at="2026-01-01T00:00:00Z")
        successful_record = dict(successful, fulltext_ok=True, figures=[{"label": "Fig. 1"}],
                                 article={"title_zh": "成功"}, ai_status="success",
                                 publishable=True, gate_reasons=[], status="published",
                                 updated_at="2026-01-01T00:00:00Z")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = parse_args([
                "--state", str(root / "state.json"),
                "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"),
                "--max-new", "1",
            ])
            with patch("automation.collector.search_europe_pmc", return_value=[failed, successful, doi_only]), \
                 patch("automation.collector.make_record", side_effect=[failed_record, successful_record]) as make_record_mock:
                summary = run(args)
            self.assertEqual(summary["selected"], 2)
            self.assertEqual(summary["published"], 1)
            self.assertEqual(summary["skipped_without_pmcid"], 1)
            self.assertEqual(make_record_mock.call_count, 2)

    def test_search_outage_preserves_existing_library_and_writes_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = {
                "doi:existing": {
                    "id": "doi:existing",
                    "title": "Existing published article",
                    "year": "2026",
                    "status": "published",
                    "article": {"title_zh": "已有文章"},
                }
            }
            (root / "state.json").write_text(json.dumps({
                "schema_version": 1,
                "records": existing,
                "attempted": {},
                "metrics_cache": {},
            }), encoding="utf-8")
            args = parse_args([
                "--state", str(root / "state.json"),
                "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"),
            ])
            outage = urllib.error.HTTPError("https://www.ebi.ac.uk/europepmc", 503, "Service Unavailable", {}, None)
            with patch("automation.collector.search_europe_pmc", side_effect=outage):
                summary = run(args)
            self.assertEqual(summary["status"], "upstream_unavailable")
            self.assertEqual(summary["published"], 0)
            self.assertEqual(summary["preserved_articles"], 1)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(len(output["articles"]), 1)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["last_run"]["status"], "upstream_unavailable")
            self.assertIn("503", report["last_run"]["search_error"])
            self.assertEqual(report["processed"], [])


if __name__ == "__main__":
    unittest.main()
