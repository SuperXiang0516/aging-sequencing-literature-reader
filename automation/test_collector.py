#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from automation.collector import (
    cache_figure_assets,
    extract_fulltext,
    easy_scholar_metrics,
    normalize_candidate,
    score_candidate,
    safe_key,
    run,
    parse_args,
    publication_gate,
    is_preservable_record,
    search_ncbi_pmc,
    pmc_aws_file_map,
    image_suffix,
    _extract_json_object,
    _llm_response_content,
    _normalize_llm_article,
    generate_llm_article,
)


BASE = Path(__file__).parent
Q1_METRICS = {
    "impact_factor": "12.3",
    "jcr": "SCI Q1",
    "jcr_quartile": "Q1",
    "jcr_q1": True,
    "status": "matched",
    "source": "EasyScholar",
    "retrieved_at": "2026-01-01T00:00:00Z",
}


class CollectorTests(unittest.TestCase):
    def test_llm_generation_retries_after_malformed_json(self):
        article = {field: "充分的中文内容" for field in (
            "title_zh", "lead", "background", "research_question", "study_design",
            "methods", "results", "limitations", "takeaways",
        )}
        article["figure_notes"] = [{"caption_zh": "图注", "interpretation": "解读"}]

        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, _size):
                return json.dumps(self.payload).encode("utf-8")

        malformed = {"choices": [{"message": {"content": "```json {not valid```"}}]}
        valid = {"choices": [{"message": {"content": json.dumps(article, ensure_ascii=False)}}]}
        record = {"title": "test", "authors": [], "figures": [{"label": "Fig. 1", "caption": "caption"}], "methods_text": "", "results_text": ""}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key", "LLM_MAX_ATTEMPTS": "2"}, clear=False), \
             patch("automation.collector.urllib.request.urlopen", side_effect=[Response(malformed), Response(valid)]):
            parsed, status = generate_llm_article(record)
        self.assertEqual(status, "success_retried")
        self.assertEqual(parsed["title_zh"], "充分的中文内容")

    def test_llm_generation_exposes_embedded_error_envelope(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, _size):
                payload = {"choices": [{"message": {"content": '{"error":"quota exceeded"}'}}]}
                return json.dumps(payload).encode("utf-8")

        record = {"title": "test", "authors": [], "figures": [], "methods_text": "", "results_text": ""}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key", "LLM_MAX_ATTEMPTS": "1"}, clear=False), \
             patch("automation.collector.urllib.request.urlopen", return_value=Response()):
            parsed, status = generate_llm_article(record)
        self.assertEqual(parsed, {})
        self.assertEqual(status, "llm_api_payload:quota exceeded")

    def test_llm_json_parser_accepts_fence_and_preamble(self):
        payload = '下面是文章 JSON：\n```json\n{"title_zh":"标题","figure_notes":[]}\n```\n'
        parsed = _extract_json_object(payload)
        self.assertEqual(parsed["title_zh"], "标题")
        self.assertEqual(parsed["figure_notes"], [])

    def test_llm_normalizer_handles_content_arrays_and_panel_aliases(self):
        article = _normalize_llm_article({
            "lead": ["第一段", "第二段"],
            "figure_notes": {"items": [{
                "图注": "完整图注", "读图": "按 a、b 顺序阅读",
                "panels": [{"面板": "a", "实验": "实验", "比较": "两组", "结果": "升高", "统计": "正文未报告"}],
                "解读": "解释", "证据": "正文", "边界": "边界",
            }]},
        }, 1)
        self.assertEqual(article["lead"], "第一段\n\n第二段")
        note = article["figure_notes"][0]
        self.assertEqual(note["caption_zh"], "完整图注")
        self.assertEqual(note["panel_reading"][0]["panel"], "a")

    def test_llm_normalizer_cleans_format_markers_but_keeps_statistical_stars(self):
        article = _normalize_llm_article({
            "methods": "第一步\\n第二步：**关键方法**；显著性* p≤0.05、** p≤0.01。",
            "figure_notes": [{"caption_zh": "`图注`", "interpretation": "__重点结论__"}],
        }, 1)
        self.assertEqual(article["methods"], "第一步\n第二步：关键方法；显著性* p≤0.05、** p≤0.01。")
        self.assertEqual(article["figure_notes"][0]["caption_zh"], "图注")
        self.assertEqual(article["figure_notes"][0]["interpretation"], "重点结论")

    def test_llm_normalizer_extracts_text_from_object_prose_items(self):
        article = _normalize_llm_article({
            "methods": [{"text": "样本处理"}, {"content": "测序分析"}],
            "results": {"text": "主要结果"},
            "figure_notes": [],
        }, 0)
        self.assertEqual(article["methods"], "样本处理\n\n测序分析")
        self.assertEqual(article["results"], "主要结果")

    def test_llm_normalizer_extracts_text_from_nested_note_and_panel_objects(self):
        article = _normalize_llm_article({
            "figure_notes": [{
                "caption_zh": {"text": "图注说明"},
                "panel_reading": [{
                    "panel": {"text": "a"},
                    "experiment": {"content": "测序实验"},
                    "comparison": "两组",
                    "result": "**显著升高**",
                    "statistics": "** p≤0.01",
                }],
            }],
        }, 1)
        note = article["figure_notes"][0]
        panel = note["panel_reading"][0]
        self.assertEqual(note["caption_zh"], "图注说明")
        self.assertEqual(panel["panel"], "a")
        self.assertEqual(panel["experiment"], "测序实验")
        self.assertEqual(panel["result"], "显著升高")
        self.assertEqual(panel["statistics"], "** p≤0.01")

    def test_llm_response_content_accepts_proxy_shapes(self):
        article = {"title_zh": "标题", "figure_notes": []}
        encoded = json.dumps(article, ensure_ascii=False)
        self.assertEqual(_llm_response_content({"choices": [{"text": encoded}]}), encoded)
        self.assertEqual(_llm_response_content({"output_text": encoded}), encoded)
        self.assertTrue(_llm_response_content({"error": {"message": "context too long"}}).startswith("__LLM_ERROR__:"))

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
        self.assertTrue(figures[0]["image_url"].startswith("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/bin/"))
        self.assertTrue(figures[0]["image_url"].endswith("/example-f1.jpg"))
        self.assertIn("Workflow", figures[0]["caption"])
        self.assertEqual(supplements[0]["label"], "Supplementary Table 1")
        self.assertTrue(supplements[0]["url"].endswith("/example-supplement.pdf"))

    def test_xml_prefers_pmc_aws_media_when_available(self):
        xml = (BASE / "fixtures/pmc-example.xml").read_text(encoding="utf-8")
        mapped = {"example-f1.jpg": "https://pmc-oa-opendata.s3.amazonaws.com/PMC1234567.1/example-f1.jpg"}
        _, _, _, figures, _ = extract_fulltext(xml, "PMC1234567", mapped)
        self.assertEqual(figures[0]["image_url"], mapped["example-f1.jpg"])

    def test_xml_excludes_graphical_abstract_from_body_figures(self):
        xml = """<article xmlns:xlink="http://www.w3.org/1999/xlink"><body>
          <fig id="ga1"><label>Graphical abstract</label><caption>Graphical abstract.</caption><graphic xlink:href="ga1.webp"/></fig>
          <fig id="F1"><label>Fig. 1</label><caption>Study workflow.</caption><graphic xlink:href="f1.jpg"/></fig>
        </body></article>"""
        _, _, _, figures, _ = extract_fulltext(xml, "PMC1234567")
        self.assertEqual([figure["label"] for figure in figures], ["Fig. 1"])

    def test_xml_caption_spacing_is_normalized_without_changing_values(self):
        xml = """<article xmlns:xlink="http://www.w3.org/1999/xlink"><body>
          <fig id="F1"><label>Fig. 1</label><caption>Panel ( A ) shows MMP2 , MMP3 after post -treatment ( n = 12 ).</caption><graphic xlink:href="f1.jpg"/></fig>
        </body></article>"""
        _, _, _, figures, _ = extract_fulltext(xml, "PMC1234567")
        self.assertEqual(figures[0]["caption"], "Panel (A) shows MMP2, MMP3 after post-treatment ( n = 12 ).")

    def test_xml_extracts_media_and_inline_graphic_body_figures(self):
        xml = """<article xmlns:xlink="http://www.w3.org/1999/xlink"><body>
          <fig id="F1"><label>Fig. 1</label><caption>Media figure.</caption><media xlink:href="f1.svg"/></fig>
          <fig id="F2"><label>Fig. 2</label><caption>Inline figure.</caption><inline-graphic xlink:href="f2.png"/></fig>
        </body></article>"""
        _, _, _, figures, _ = extract_fulltext(xml, "PMC1234567")
        self.assertEqual([figure["image_url"].rsplit("/", 1)[-1] for figure in figures], ["f1.svg", "f2.png"])

    def test_image_suffix_accepts_svg_xml_declaration(self):
        self.assertEqual(image_suffix(b"<?xml version=\"1.0\"?><svg xmlns=\"http://www.w3.org/2000/svg\"></svg>", "fig.bin"), ".svg")

    def test_llm_normalizer_unwraps_chinese_article_keys(self):
        article = _normalize_llm_article({
            "文章": {"标题": "标题", "方法": "方法", "图组解读": []},
            "lead": "导语",
        }, 0)
        # The wrapper is intentionally only one of the supported aliases; the
        # test also verifies that Chinese top-level labels are mapped before
        # the production schema gate runs.
        self.assertEqual(article["title_zh"], "标题")
        self.assertEqual(article["methods"], "方法")

    def test_pmc_aws_file_map_selects_only_image_objects(self):
        listing = """<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <Contents><Key>PMC1234567.1/PMC1234567.1.xml</Key></Contents>
          <Contents><Key>PMC1234567.1/example-f1.jpg</Key></Contents>
          <Contents><Key>PMC1234567.1/supplement.pdf</Key></Contents>
        </ListBucketResult>"""
        with patch("automation.collector.http_text", return_value=listing):
            mapped = pmc_aws_file_map("PMC1234567")
        self.assertEqual(list(mapped), ["example-f1.jpg"])
        self.assertTrue(mapped["example-f1.jpg"].endswith("/PMC1234567.1/example-f1.jpg"))

    def test_cache_figure_assets_writes_validated_local_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            figures = [{"label": "Fig. 1", "caption": "caption", "image_url": "https://example.org/fig-1.png"}]
            png = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
            with patch("automation.collector._http_read", return_value=png):
                cached, errors = cache_figure_assets(figures, "PMC123456", root / "assets")
            self.assertEqual(errors, [])
            self.assertEqual(cached[0]["asset_status"], "downloaded")
            self.assertTrue(cached[0]["local_url"].endswith("generated/assets/PMC123456/fig-01.png"))
            self.assertEqual(cached[0]["asset_bytes"], len(png))
            self.assertTrue((root / "assets/PMC123456/fig-01.png").exists())

    def test_cache_figure_assets_rejects_html_error_page(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            figures = [{"label": "Fig. 1", "caption": "caption", "image_url": "https://example.org/fig-1.png"}]
            with patch("automation.collector._http_read", return_value=b"<html>blocked</html>"):
                cached, errors = cache_figure_assets(figures, "PMC123456", root / "assets")
            self.assertEqual(len(errors), 1)
            self.assertEqual(cached[0]["asset_status"], "error")
            self.assertFalse((root / "assets/PMC123456/fig-01.png").exists())

    def test_cache_figure_assets_uses_aws_source_when_legacy_url_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            figures = [{"label": "Fig. 1", "caption": "caption", "image_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/bin/example-f1.jpg"}]
            png = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
            with patch("automation.collector.download_image", return_value={"path": root / "assets/PMC123456/fig-01.png", "bytes": len(png), "sha256": "abc", "suffix": ".png"}), \
                 patch("automation.collector.pmc_aws_file_map", return_value={"example-f1.jpg": "https://pmc-oa-opendata.s3.amazonaws.com/PMC123456.1/example-f1.jpg"}):
                cached, errors = cache_figure_assets(figures, "PMC123456", root / "assets")
            self.assertEqual(errors, [])
            self.assertEqual(cached[0]["original_url"], "https://pmc-oa-opendata.s3.amazonaws.com/PMC123456.1/example-f1.jpg")
            self.assertEqual(cached[0]["source_url"], "https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/bin/example-f1.jpg")

    def test_run_publishes_only_after_local_assets_are_cached(self):
        article = {
            "title_zh": "长读长宏基因组揭示年龄相关肠道基因组",
            "lead": "这是一段来源可追溯的导语，用于说明研究对象、核心发现、关键方法和读者为什么需要继续阅读。" * 2,
            "background": "背景说明" * 30,
            "research_question": "研究问题" * 20,
            "study_design": "研究设计" * 30,
            "methods": "完整方法翻译，包含样本、测序平台、质控、统计和数据处理步骤。" * 20,
            "results": "按正文翻译的结果，逐步说明比较对象、方向、统计证据和结论边界。" * 20,
            "limitations": "局限与外推边界" * 12,
            "takeaways": "对后续研究的启示" * 12,
            "figure_notes": [{
                "label": "图1",
                "caption_zh": "图注翻译：展示研究流程、关键比较和主要结果。",
                "what_question": "这张图回答研究问题。",
                "how_to_read": "先看实验分组，再按 panel 顺序比较方向，最后结合正文统计量判断证据强度。" * 3,
                "panel_reading": [{"panel": "a", "experiment": "实验流程", "comparison": "两组比较", "result": "正文报告的主要结果", "statistics": "正文未报告"}],
                "interpretation": "结果解读" * 30,
                "evidence": "正文图注与结果段共同支持该结论。",
                "boundary": "该结果不应外推为普遍因果关系。",
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = parse_args([
                "--fixture", str(BASE / "fixtures/europepmc.json"),
                "--xml-fixture", str(BASE / "fixtures/pmc-example.xml"),
                "--assets-dir", str(root / "assets"),
                "--state", str(root / "state.json"),
                "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"),
            ])
            png = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
            with patch("automation.collector.generate_llm_article", return_value=(article, "success")), \
                 patch("automation.collector.easy_scholar_metrics", return_value=Q1_METRICS), \
                 patch("automation.collector._http_read", return_value=png):
                summary = run(args)
                second = run(args)
            self.assertEqual(summary["published"], 1)
            self.assertEqual(second["selected"], 0)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            figure = output["articles"][0]["figures"][0]
            self.assertEqual(figure["asset_status"], "downloaded")
            self.assertTrue(figure["local_url"].startswith("generated/assets/PMC1234567/"))
            self.assertTrue((root / "assets/PMC1234567/fig-01.png").exists())

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
            with patch.dict(os.environ, {"EASYSCHOLAR_SECRET_KEY": ""}, clear=False), \
                 patch("automation.collector.easy_scholar_metrics", return_value=Q1_METRICS):
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
            with patch("automation.collector.generate_llm_article", return_value=(article, "success")), \
                 patch("automation.collector.easy_scholar_metrics", return_value=Q1_METRICS):
                summary = run(args)
            self.assertEqual(summary["published"], 1)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(output["articles"][0]["authors"], ["A. Example", "B. Example"])
            self.assertIn("Example Aging Journal", output["articles"][0]["journal_display"])
            self.assertEqual(output["articles"][0]["status"], "published")
            self.assertNotIn("fulltext_text", output["articles"][0])
            self.assertNotIn("methods_text", output["articles"][0])
            self.assertNotIn("supplements", output["articles"][0])

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
            with patch("automation.collector.generate_llm_article", return_value=(article, "success")), \
                 patch("automation.collector.easy_scholar_metrics", return_value=Q1_METRICS):
                self.assertEqual(run(args)["published"], 1)
                self.assertEqual(run(args)["selected"], 0)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(len(output["articles"]), 1)

    def test_legacy_preservation_drops_old_out_of_scope_records(self):
        base = {
            "metrics": Q1_METRICS,
            "publishable": True,
            "fulltext_ok": True,
            "figures": [{"label": "Fig. 1", "caption": "caption", "image_url": "https://example.org/fig.jpg"}],
            "article": {
                "title_zh": "标题", "lead": "导语", "background": "背景", "research_question": "问题",
                "study_design": "设计", "methods": "方法", "results": "结果", "limitations": "局限", "takeaways": "启示",
                "figure_notes": [{
                    "caption_zh": "图注", "what_question": "问题", "how_to_read": "读图", "interpretation": "解读",
                    "evidence": "证据", "boundary": "边界",
                    "panel_reading": [{"panel": "a", "experiment": "实验", "comparison": "比较", "result": "结果", "statistics": "统计"}],
                }],
            },
        }
        self.assertFalse(is_preservable_record({
            **base,
            "title": "Fast, sensitive and accurate integration of single-cell data with Harmony",
            "relevance_evidence": ["sequencing: single-cell"],
        }))
        self.assertFalse(is_preservable_record({
            **base,
            "title": "Tissue engineering polymeric formulations enable lymphangiogenesis for myocardial repair",
            "relevance_evidence": ["sequencing: rna sequencing", "disease_bridge: cardiovascular"],
        }))
        self.assertTrue(is_preservable_record({
            **base,
            "title": "Ageing-associated long-read metagenomics reveals gut microbial genomes",
            "relevance_evidence": ["aging: ageing", "sequencing: long-read, metagenom"],
        }))

    def test_run_skips_doi_only_hits_and_continues_after_failed_candidate(self):
        def candidate(identifier, pmcid):
            return {
                "id": identifier,
                "title": f"Ageing long-read sequencing study {identifier}",
                "authors": [],
                "journal": "Example Journal",
                "year": "2026",
                "doi": identifier,
                "pmcid": pmcid,
                "pmid": "",
                "abstract": "ageing long-read sequencing data and bioinformatic analysis",
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
                    "publishable": True,
                    "fulltext_ok": True,
                    "figures": [{"label": "Fig. 1", "caption": "caption", "image_url": "https://example.org/fig1.jpg"}],
                    "article": {"title_zh": "已有文章"},
                    "metrics": Q1_METRICS,
                },
                "doi:legacy-without-figure": {
                    "id": "doi:legacy-without-figure",
                    "title": "Legacy record without an original figure",
                    "status": "published",
                    "publishable": True,
                    "metrics": Q1_METRICS,
                    "article": {"title_zh": "旧记录"},
                },
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
            # This test covers the case where both providers are unavailable;
            # keep it deterministic by blocking the NCBI fallback as well.
            with patch("automation.collector.search_europe_pmc", side_effect=outage), \
                 patch("automation.collector.search_ncbi_pmc", side_effect=outage):
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

    def test_legacy_remote_record_is_dropped_when_local_asset_migration_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = {
                "id": "doi:legacy",
                "title": "Legacy published article",
                "year": "2026",
                "status": "published",
                "publishable": True,
                "fulltext_ok": True,
                "figures": [{"label": "Fig. 1", "caption": "caption", "image_url": "https://example.org/fig1.jpg"}],
                "article": {"title_zh": "已有文章"},
                "metrics": Q1_METRICS,
            }
            (root / "state.json").write_text(json.dumps({"schema_version": 1, "records": {"doi:legacy": legacy}, "attempted": {}, "metrics_cache": {}}), encoding="utf-8")
            args = parse_args([
                "--state", str(root / "state.json"), "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"), "--assets-dir", str(root / "assets"),
            ])
            outage = urllib.error.HTTPError("https://www.ebi.ac.uk/europepmc", 503, "Service Unavailable", {}, None)
            migrated = [dict(legacy["figures"][0], asset_status="error", asset_error="HTTPError")]
            with patch("automation.collector.cache_figure_assets", return_value=(migrated, ["fig-01:HTTPError"])), \
                 patch("automation.collector.search_europe_pmc", side_effect=outage), \
                 patch("automation.collector.search_ncbi_pmc", side_effect=outage):
                summary = run(args)
            self.assertEqual(summary["preserved_articles"], 0)
            output = json.loads((root / "articles.json").read_text(encoding="utf-8"))
            self.assertEqual(len(output["articles"]), 0)

    def test_ncbi_summary_is_normalized_to_pmc_candidate(self):
        payloads = [
            {"esearchresult": {"idlist": ["123456"]}},
            {"result": {
                "uids": ["123456"],
                "123456": {
                    "uid": "123456",
                    "title": "Long-read metagenomics of ageing",
                    "fulljournalname": "Example Genomics",
                    "pubdate": "2026 Jan",
                    "authors": [{"name": "A. Example"}],
                    "articleids": [
                        {"idtype": "pmc", "value": "PMC123456"},
                        {"idtype": "doi", "value": "10.0000/example"},
                    ],
                },
            }},
        ]
        with patch("automation.collector.ncbi_json", side_effect=payloads):
            records = search_ncbi_pmc("aging AND nanopore", page_size=1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pmcid"], "PMC123456")
        self.assertEqual(records[0]["journal"], "Example Genomics")
        self.assertEqual(records[0]["authors"], ["A. Example"])
        self.assertGreaterEqual(records[0]["relevance_score"], 12)

    def test_ncbi_query_match_is_not_dropped_when_esummary_has_no_abstract(self):
        payloads = [
            {"esearchresult": {"idlist": ["123456"]}},
            {"result": {
                "uids": ["123456"],
                "123456": {
                    "uid": "123456",
                    "title": "A paper whose relevant terms are only in the indexed abstract",
                    "fulljournalname": "Example Genomics",
                    "pubdate": "2026",
                    "articleids": [{"idtype": "pmcid", "value": "PMC123456"}],
                },
            }},
        ]
        with patch("automation.collector.ncbi_json", side_effect=payloads):
            records = search_ncbi_pmc("aging AND nanopore", page_size=1)
        self.assertEqual(records[0]["relevance_score"], 12)
        self.assertIn("完整查询命中", records[0]["relevance_evidence"][-1])

    def test_ncbi_fallback_translates_europe_pmc_open_access_syntax(self):
        payloads = [
            {"esearchresult": {"idlist": []}},
        ]
        captured = []

        def fake_ncbi(endpoint, params, **kwargs):
            captured.append((endpoint, params))
            return payloads.pop(0)

        with patch("automation.collector.ncbi_json", side_effect=fake_ncbi):
            search_ncbi_pmc("OPEN_ACCESS:Y AND ((aging) AND (nanopore))", page_size=1)
        self.assertEqual(len(captured), 1)
        term = captured[0][1]["term"]
        self.assertNotIn("OPEN_ACCESS", term.upper())
        self.assertIn("open access[filter]", term)

    def test_candidate_scope_requires_topic_and_omics_signal(self):
        from automation.collector import candidate_matches_scope
        self.assertTrue(candidate_matches_scope({"title": "Ageing-associated methylome sequencing", "abstract": "senescence and long-read sequencing"}))
        self.assertTrue(candidate_matches_scope({"title": "Diabetes nanopore benchmark", "abstract": "nanopore sequencing data and a bioinformatic workflow"}))
        self.assertFalse(candidate_matches_scope({"title": "A highly cited metagenomics paper", "abstract": "healthy microbiome"}))
        self.assertFalse(candidate_matches_scope({
            "title": "Tissue engineering polymeric formulations enable lymphangiogenesis for myocardial repair",
            "abstract": "Single-cell RNA sequencing was used as one validation assay in a biomaterials study.",
        }))
        self.assertFalse(candidate_matches_scope({
            "title": "Age-associated inflammation in older adults",
            "abstract": "RNA sequencing was briefly mentioned, without sequencing data analysis or an omics study design.",
        }))
        self.assertFalse(candidate_matches_scope({
            "title": "Aging hydrogel therapy",
            "abstract": "Single-cell RNA sequencing was used to validate treatment efficacy.",
        }))

    def test_europe_search_defaults_to_newest_first(self):
        payload = {"resultList": {"result": []}}
        with patch("automation.collector.http_json", return_value=payload) as request:
            from automation.collector import search_europe_pmc
            search_europe_pmc("aging AND nanopore", page_size=5)
        query_url = request.call_args.args[0]
        self.assertIn("P_PDATE_D+desc", query_url)

    def test_publication_gate_requires_jcr_q1(self):
        record = {
            "metrics": {
                "impact_factor": "4.1",
                "jcr": "SCI Q2",
                "jcr_quartile": "Q2",
                "jcr_q1": False,
            },
            "fulltext_ok": True,
            "figures": [{"label": "Fig. 1", "caption": "caption"}],
            "methods_text": "methods",
            "results_text": "results",
        }
        article = {
            "title_zh": "标题",
            "background": "背景",
            "methods": "方法",
            "results": "结果",
            "figure_notes": [{"caption_zh": "图注", "interpretation": "解读"}],
        }
        publishable, reasons = publication_gate(record, article)
        self.assertFalse(publishable)
        self.assertIn("期刊未确认属于 JCR/SCI 1 区", reasons)

    def test_production_gate_rejects_short_article_and_incomplete_figure_note(self):
        record = {
            "metrics": {"jcr_q1": True},
            "assets_required": True,
            "fulltext_ok": True,
            "figures": [{"label": "Fig. 1", "caption": "caption", "image_url": "https://example.org/fig.jpg", "local_url": "generated/assets/PMC1/fig-01.jpg", "asset_status": "downloaded"}],
            "methods_text": "methods",
            "results_text": "results",
        }
        article = {
            "title_zh": "标题", "lead": "短", "background": "短", "research_question": "短", "study_design": "短",
            "methods": "短", "results": "短", "limitations": "短", "takeaways": "短",
            "figure_notes": [{"caption_zh": "图注", "interpretation": "一句话"}, {"caption_zh": "多余图", "interpretation": "多余解读"}],
        }
        publishable, reasons = publication_gate(record, article)
        self.assertFalse(publishable)
        self.assertIn("AI 内容过短：methods", reasons)
        self.assertIn("AI 图组解读数量与正文原图数量不一致", reasons)

    def test_easyscholar_sci_q1_is_normalized_for_the_gate(self):
        payload = {"data": {"officialRank": {"all": {"sciif": "12.3", "sci": "Q1"}}}}
        with patch.dict(os.environ, {"EASYSCHOLAR_SECRET_KEY": "test-secret"}, clear=False), \
             patch("automation.collector.http_json", return_value=payload):
            metrics = easy_scholar_metrics("Example Aging Journal", {})
        self.assertEqual(metrics["impact_factor"], "12.3")
        self.assertEqual(metrics["jcr_quartile"], "Q1")
        self.assertTrue(metrics["jcr_q1"])

    def test_easyscholar_empty_rank_response_does_not_abort_collection(self):
        with patch.dict(os.environ, {"EASYSCHOLAR_SECRET_KEY": "test-secret"}, clear=False), \
             patch("automation.collector.http_json", return_value={"data": {"officialRank": {"all": None}}}):
            metrics = easy_scholar_metrics("Example Aging Journal", {})
        self.assertEqual(metrics["status"], "not_found")
        self.assertFalse(metrics["jcr_q1"])

    def test_europe_outage_uses_ncbi_fallback(self):
        candidate = {
            "id": "pmcid:PMC123456",
            "title": "Long-read metagenomics of ageing",
            "authors": ["A. Example"],
            "journal": "Example Genomics",
            "year": "2026",
            "doi": "10.0000/example",
            "pmcid": "PMC123456",
            "pmid": "",
            "abstract": "Ageing and nanopore sequencing.",
            "fulltext_urls": [],
            "source": "NCBI PMC",
            "relevance_score": 20,
            "relevance_evidence": ["aging: ageing", "sequencing: nanopore"],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = parse_args([
                "--state", str(root / "state.json"),
                "--output", str(root / "articles.json"),
                "--report", str(root / "report.json"),
                "--max-new", "1",
            ])
            outage = urllib.error.HTTPError("https://www.ebi.ac.uk/europepmc", 503, "Service Unavailable", {}, None)
            with patch("automation.collector.search_europe_pmc", side_effect=outage), \
                 patch("automation.collector.search_ncbi_pmc", return_value=[candidate]), \
                 patch("automation.collector.make_record", return_value={**candidate, "publishable": False, "status": "discarded", "gate_reasons": ["test"], "updated_at": "2026-01-01T00:00:00Z"}):
                summary = run(args)
            self.assertEqual(summary["status"], "fallback_ncbi")
            self.assertEqual(summary["candidates"], 1)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertIn("Europe PMC HTTP 503", report["last_run"]["search_error"])


if __name__ == "__main__":
    unittest.main()
