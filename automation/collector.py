#!/usr/bin/env python3
"""Daily literature-to-article pipeline for the paper plot library.

The pipeline is deliberately conservative:

* candidates are scored for ageing + sequencing/multi-omics relevance;
* only records with retrievable full text and original figure captions can
  pass the publication gate;
* an LLM produces a structured Chinese draft, but it is never allowed to
  invent missing values; and
* failed/incomplete candidates are kept in the run report, not shown as
  published articles.

The module uses the Python standard library so GitHub Actions can run it
without installing a scientific Python stack. Network calls are bounded and
all generated data are JSON, which makes every daily change reviewable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


SCHEMA_VERSION = 1
DEFAULT_QUERY = (
    '((aging OR ageing OR senescence OR geroscience OR "age-related" OR '
    'lifespan OR diabetes OR "metabolic syndrome" OR obesity) AND '
    '("long-read" OR "long read" OR nanopore OR PacBio OR HiFi '
    'OR "third-generation sequencing" OR metagenom* OR multi-omics OR '
    'multiomics OR "single-cell" OR "RNA sequencing"))'
)
EUROPE_PMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EASYSCHOLAR_URL = "https://www.easyscholar.cc/open/getPublicationRank"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", html.unescape(str(value))).strip()
    return text


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return clean_text(" ".join(node.itertext()))


def first_child(node: ET.Element, name: str) -> Optional[ET.Element]:
    for child in node.iter():
        if local_name(child.tag) == name:
            return child
    return None


def safe_key(record: Mapping[str, Any]) -> str:
    doi = clean_text(record.get("doi")).lower()
    if doi:
        return f"doi:{doi}"
    pmcid = clean_text(record.get("pmcid")).lower()
    if pmcid:
        return f"pmcid:{pmcid}"
    pmid = clean_text(record.get("pmid"))
    return f"pmid:{pmid}" if pmid else f"title:{clean_text(record.get('title')).lower()}"


def author_names(item: Mapping[str, Any]) -> list[str]:
    authors = item.get("authorList", {}).get("author", []) if isinstance(item.get("authorList"), Mapping) else []
    if isinstance(authors, Mapping):
        authors = [authors]
    output: list[str] = []
    for author in authors or []:
        if not isinstance(author, Mapping):
            continue
        name = clean_text(author.get("fullName") or author.get("authorName"))
        if name:
            output.append(name)
    if output:
        return output
    raw = clean_text(item.get("authorString"))
    return [part.strip() for part in raw.split(",") if part.strip()]


def normalize_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    doi = clean_text(item.get("doi"))
    pmcid = clean_text(item.get("pmcid"))
    pmid = clean_text(item.get("pmid"))
    year = clean_text(item.get("pubYear")) or clean_text(item.get("firstPublicationDate"))[:4]
    fulltext_urls = item.get("fullTextUrlList", {}).get("fullTextUrl", []) if isinstance(item.get("fullTextUrlList"), Mapping) else []
    if isinstance(fulltext_urls, Mapping):
        fulltext_urls = [fulltext_urls]
    journal_info = item.get("journalInfo") if isinstance(item.get("journalInfo"), Mapping) else {}
    journal_from_info = journal_info.get("journal", {}).get("title") if isinstance(journal_info.get("journal"), Mapping) else ""
    return {
        "id": safe_key({"doi": doi, "pmcid": pmcid, "pmid": pmid, "title": item.get("title")}),
        "title": clean_text(item.get("title")),
        "authors": author_names(item),
        "journal": clean_text(item.get("journalTitle") or journal_from_info),
        "year": year,
        "doi": doi,
        "pmcid": pmcid,
        "pmid": pmid,
        "abstract": clean_text(item.get("abstractText")),
        "citation_count": int(item.get("citedByCount") or 0),
        "fulltext_urls": [clean_text(u.get("url")) for u in fulltext_urls if isinstance(u, Mapping) and clean_text(u.get("url"))],
        "source": "Europe PMC",
    }


def score_candidate(record: Mapping[str, Any]) -> tuple[int, list[str]]:
    text = f"{record.get('title', '')} {record.get('abstract', '')}".lower()
    groups = {
        "aging": (6, ["aging", "ageing", "senescence", "geroscience", "age-related", "lifespan", "inflammaging", "衰老"]),
        "sequencing": (7, ["long-read", "long read", "nanopore", "pacbio", "hifi", "third-generation sequencing", "metagenom", "rna sequencing", "single-cell"]),
        "multiomics": (4, ["multi-omics", "multiomics", "transcriptom", "methylom", "proteom", "metabolom", "epigenom"]),
        "tools": (3, ["assembler", "benchmark", "pipeline", "bioinformatic", "software", "database", "workflow"]),
        "disease_bridge": (5, ["diabetes", "metabolic syndrome", "obesity", "cardiovascular", "kidney disease"]),
    }
    score = 0
    evidence: list[str] = []
    for group, (weight, keywords) in groups.items():
        hits = [word for word in keywords if word in text]
        if hits:
            score += weight
            evidence.append(f"{group}: {', '.join(hits[:4])}")
    score += min(int(record.get("citation_count") or 0), 50) // 10
    return min(score, 100), evidence


def http_json(url: str, *, timeout: int = 30, headers: Optional[Mapping[str, str]] = None) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "paper-plot-library/1.0", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(8 * 1024 * 1024)
    return json.loads(payload.decode("utf-8"))


def http_text(url: str, *, timeout: int = 40) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "paper-plot-library/1.0", "Accept": "application/xml,text/xml,text/plain"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(20 * 1024 * 1024).decode("utf-8", errors="replace")


def search_europe_pmc(query: str, *, page_size: int = 40, fixture: Optional[Path] = None) -> list[dict[str, Any]]:
    if fixture:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
    else:
        params = urllib.parse.urlencode({"query": query, "format": "json", "pageSize": page_size, "resultType": "core", "sort": "CITED desc"})
        payload = http_json(f"{EUROPE_PMC_SEARCH}?{params}")
    result_list = payload.get("resultList", {}).get("result", []) if isinstance(payload, Mapping) else []
    if isinstance(result_list, Mapping):
        result_list = [result_list]
    records = []
    for item in result_list:
        if isinstance(item, Mapping):
            normalized = normalize_candidate(item)
            normalized["relevance_score"], normalized["relevance_evidence"] = score_candidate(normalized)
            records.append(normalized)
    return sorted(records, key=lambda x: (x["relevance_score"], x["citation_count"], x["year"]), reverse=True)


def pmc_xml_url(pmcid: str) -> str:
    return f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid)}/fullTextXML"


def figure_url(pmcid: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{urllib.parse.quote(pmcid)}/bin/{urllib.parse.quote(href)}"


def extract_fulltext(xml_text: str, pmcid: str) -> tuple[str, str, str, list[dict[str, str]], list[dict[str, str]]]:
    root = ET.fromstring(xml_text)
    paragraphs: list[str] = []
    methods: list[str] = []
    results: list[str] = []
    figures: list[dict[str, str]] = []
    supplements: list[dict[str, str]] = []
    for section in root.iter():
        if local_name(section.tag) != "sec":
            continue
        title_node = first_child(section, "title")
        title = element_text(title_node)
        text = element_text(section)
        if not text:
            continue
        paragraphs.append(f"{title}: {text}" if title else text)
        lowered = title.lower()
        if any(token in lowered for token in ("method", "material", "experimental", "protocol")):
            methods.append(text)
        if "result" in lowered:
            results.append(text)
    for fig in root.iter():
        if local_name(fig.tag) != "fig":
            continue
        label = element_text(first_child(fig, "label"))
        caption = element_text(first_child(fig, "caption"))
        href = ""
        for node in fig.iter():
            if local_name(node.tag) == "graphic":
                href = clean_text(node.attrib.get("{http://www.w3.org/1999/xlink}href") or node.attrib.get("href"))
                if href:
                    break
        if href:
            figures.append({"label": label or f"Figure {len(figures) + 1}", "caption": caption, "image_url": figure_url(pmcid, href)})
    for node in root.iter():
        if local_name(node.tag) != "supplementary-material":
            continue
        label = element_text(first_child(node, "label"))
        href = clean_text(node.attrib.get("{http://www.w3.org/1999/xlink}href") or node.attrib.get("href"))
        for child in node.iter():
            if not href and local_name(child.tag) in {"media", "graphic"}:
                href = clean_text(child.attrib.get("{http://www.w3.org/1999/xlink}href") or child.attrib.get("href"))
        if href:
            supplements.append({"label": label or f"Supplementary file {len(supplements) + 1}", "url": figure_url(pmcid, href), "description": element_text(first_child(node, "caption"))})
    return "\n\n".join(paragraphs), "\n\n".join(methods), "\n\n".join(results), figures, supplements


def easy_scholar_metrics(journal: str, cache: dict[str, Any]) -> dict[str, str]:
    journal = clean_text(journal)
    empty = {"impact_factor": "", "jcr": "", "status": "unavailable_no_secret", "source": "EasyScholar", "retrieved_at": ""}
    if not journal:
        empty["status"] = "missing_journal"
        return empty
    cache_key = journal.casefold()
    if cache_key in cache:
        return cache[cache_key]
    secret = clean_text(os.environ.get("EASYSCHOLAR_SECRET_KEY"))
    if not secret:
        cache[cache_key] = empty
        return empty
    params = urllib.parse.urlencode({"secretKey": secret, "publicationName": journal})
    try:
        payload = http_json(f"{EASYSCHOLAR_URL}?{params}", timeout=20)
        ranks = payload.get("data", {}).get("officialRank", {}).get("all", {}) if isinstance(payload, Mapping) else {}
        sci = clean_text(ranks.get("sci"))
        ssci = clean_text(ranks.get("ssci"))
        jcr = " / ".join(x for x in (f"SCI {sci}" if sci else "", f"SSCI {ssci}" if ssci else "") if x)
        result = {"impact_factor": clean_text(ranks.get("sciif")), "jcr": jcr, "status": "matched" if (ranks.get("sciif") or jcr) else "not_found", "source": "EasyScholar", "retrieved_at": utc_now()}
    except (OSError, ValueError, KeyError, TypeError):
        result = {**empty, "status": "error", "retrieved_at": utc_now()}
    cache[cache_key] = result
    return result


def metric_label(metrics: Mapping[str, Any]) -> str:
    parts = []
    if clean_text(metrics.get("impact_factor")):
        parts.append(f"IF {clean_text(metrics['impact_factor'])}")
    if clean_text(metrics.get("jcr")):
        parts.append(f"JCR {clean_text(metrics['jcr'])}")
    return "；".join(parts) or "指标待查"


def llm_prompt(record: Mapping[str, Any]) -> str:
    figures = "\n".join(f"{f.get('label')}: {f.get('caption')}" for f in record.get("figures", []))
    return f"""你是严谨的生物医学文献编辑。请把下面的论文材料写成中文公众号式深度解读，不能补造材料中没有的数字、实验或结论。首次出现的缩写写出英文全称，后文使用缩写。结果部分按正文逻辑解释：观察到什么、怎么读图、支持什么、不能支持什么。方法部分要把正文方法翻译并适当压缩，但保留样本、平台、关键软件、阈值和统计方法。只返回 JSON，不要 Markdown 代码围栏，字段必须完整：title_zh, lead, background, research_question, study_design, methods, results, limitations, takeaways, figure_notes。figure_notes 是数组，每项包含 label、caption_zh、interpretation。\n\n题目：{record.get('title')}\n作者：{', '.join(record.get('authors', []))}\n期刊：{record.get('journal')}\n摘要：{record.get('abstract')}\n正文方法：{record.get('methods_text', '')[:24000]}\n正文结果：{record.get('results_text', '')[:30000]}\n正文图注：{figures[:16000]}"""


def generate_llm_article(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    api_key = clean_text(os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
    if not api_key:
        return {}, "missing_llm_key"
    endpoint = clean_text(os.environ.get("LLM_API_URL")) or "https://api.deepseek.com/chat/completions"
    body = json.dumps({"model": os.environ.get("LLM_MODEL", "deepseek-chat"), "temperature": 0.15, "messages": [{"role": "system", "content": "You produce source-grounded scientific Chinese article JSON."}, {"role": "user", "content": llm_prompt(record)}]}).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "paper-plot-library/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
        article = json.loads(content)
        required = ("title_zh", "lead", "background", "research_question", "study_design", "methods", "results", "limitations", "takeaways", "figure_notes")
        if not all(article.get(field) for field in required):
            return {}, "llm_schema_incomplete"
        return article, "success"
    except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return {}, f"llm_error:{type(exc).__name__}"


def publication_gate(record: Mapping[str, Any], article: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not record.get("fulltext_ok"):
        reasons.append("正文全文未成功提取")
    if len(record.get("figures", [])) == 0:
        reasons.append("没有可追溯的正文原图")
    if any(not clean_text(fig.get("caption")) for fig in record.get("figures", [])):
        reasons.append("存在缺失图注")
    if not clean_text(record.get("methods_text")):
        reasons.append("正文 Methods 未提取")
    if not clean_text(record.get("results_text")):
        reasons.append("正文 Results 未提取")
    for field in ("title_zh", "background", "methods", "results", "figure_notes"):
        if not article.get(field):
            reasons.append(f"AI 字段缺失：{field}")
    notes = article.get("figure_notes")
    if isinstance(notes, list) and len(notes) < len(record.get("figures", [])):
        reasons.append("AI 图组解读数量少于正文原图数量")
    if isinstance(notes, list):
        for index, note in enumerate(notes[: len(record.get("figures", []))]):
            if not isinstance(note, Mapping) or not clean_text(note.get("caption_zh")) or not clean_text(note.get("interpretation")):
                reasons.append(f"第 {index + 1} 张图的中文图注或解读不完整")
    return not reasons, reasons


def make_record(candidate: dict[str, Any], metrics_cache: dict[str, Any], xml_fixture: Optional[Path] = None) -> dict[str, Any]:
    record = dict(candidate)
    xml_text = ""
    try:
        if xml_fixture:
            xml_text = xml_fixture.read_text(encoding="utf-8")
        elif record.get("pmcid"):
            xml_text = http_text(pmc_xml_url(record["pmcid"]))
        if xml_text and record.get("pmcid"):
            fulltext, methods, results, figures, supplements = extract_fulltext(xml_text, record["pmcid"])
            record.update({"fulltext_text": fulltext, "methods_text": methods, "results_text": results or fulltext, "figures": figures, "supplements": supplements, "fulltext_ok": bool(fulltext)})
        else:
            record.update({"fulltext_text": "", "methods_text": "", "results_text": "", "figures": [], "supplements": [], "fulltext_ok": False})
    except (OSError, ET.ParseError, urllib.error.URLError, ValueError):
        record.update({"fulltext_text": "", "methods_text": "", "results_text": "", "figures": [], "supplements": [], "fulltext_ok": False})
    record["metrics"] = easy_scholar_metrics(record.get("journal", ""), metrics_cache)
    record["journal_display"] = f"{record.get('journal', '')}（{metric_label(record['metrics'])}）"
    record["updated_at"] = utc_now()
    # Do not spend an LLM request on records that cannot provide the source
    # material required by the publication gate.  This is especially important
    # for Europe PMC hits that have a DOI/abstract but no PMCID.
    if record.get("fulltext_ok") and record.get("figures"):
        article, ai_status = generate_llm_article(record)
    else:
        article, ai_status = {}, "skipped_no_retrievable_fulltext"
    record["article"] = article
    record["ai_status"] = ai_status
    record["publishable"], record["gate_reasons"] = publication_gate(record, article)
    record["status"] = "published" if record["publishable"] else ("manual_review" if record["fulltext_ok"] and record["figures"] else "discarded")
    return record


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def public_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    """Keep publishable metadata and generated prose, never raw full text."""
    excluded = {"abstract", "fulltext_text", "methods_text", "results_text"}
    return {key: value for key, value in record.items() if key not in excluded}


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = load_json(args.state, {"schema_version": SCHEMA_VERSION, "records": {}, "attempted": {}, "metrics_cache": {}})
    records: dict[str, Any] = {
        key: public_projection(value)
        for key, value in (state.get("records", {}) if isinstance(state.get("records"), Mapping) else {}).items()
        if isinstance(value, Mapping)
    }
    attempted: dict[str, Any] = state.get("attempted", {}) if isinstance(state.get("attempted"), Mapping) else {}
    metrics_cache: dict[str, Any] = state.get("metrics_cache", {}) if isinstance(state.get("metrics_cache"), Mapping) else {}
    candidates = search_europe_pmc(args.query, page_size=args.candidate_limit, fixture=args.fixture)
    def eligible(candidate: Mapping[str, Any]) -> bool:
        if candidate["relevance_score"] < args.min_score or candidate["id"] in records:
            return False
        previous = attempted.get(candidate["id"])
        if not isinstance(previous, Mapping):
            return True
        # A temporary LLM/API failure may be retried for a bounded number of
        # runs; a paper without retrievable full text/figures is discarded.
        return previous.get("status") == "manual_review" and int(previous.get("attempts", 0)) < 5
    eligible_candidates = [candidate for candidate in candidates if eligible(candidate)]
    # A PMCID is required for the XML/full-text path used by this project.
    # DOI-only records are kept out of the daily processing quota so that one
    # inaccessible paper cannot consume the whole run.
    candidates = [candidate for candidate in eligible_candidates if clean_text(candidate.get("pmcid"))]
    skipped_without_pmcid = len(eligible_candidates) - len(candidates)
    attempt_limit = args.attempt_limit or max(args.max_new * 3, args.max_new)
    selected: list[dict[str, Any]] = []
    processed: list[dict[str, Any]] = []
    published_this_run = 0
    for candidate in candidates[:attempt_limit]:
        if published_this_run >= args.max_new:
            break
        selected.append(candidate)
        record = make_record(candidate, metrics_cache, args.xml_fixture)
        processed.append(record)
        previous_attempts = int(attempted.get(record["id"], {}).get("attempts", 0)) if isinstance(attempted.get(record["id"]), Mapping) else 0
        attempted[record["id"]] = {"status": record["status"], "attempts": previous_attempts + 1, "updated_at": record["updated_at"], "gate_reasons": record["gate_reasons"]}
        if record["publishable"]:
            records[record["id"]] = public_projection(record)
            published_this_run += 1
    published = sorted(records.values(), key=lambda r: (r.get("year", ""), r.get("updated_at", "")), reverse=True)
    state.update({"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "records": records, "attempted": attempted, "metrics_cache": metrics_cache, "last_run": {"selected": len(selected), "published": published_this_run, "candidates": len(candidates), "skipped_without_pmcid": skipped_without_pmcid, "finished_at": utc_now()}})
    save_json(args.state, state)
    save_json(args.output, {"schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "articles": [public_projection(record) for record in published]})
    report_records = [{key: value for key, value in record.items() if key not in {"abstract", "fulltext_text", "methods_text", "results_text", "article"}} for record in processed]
    save_json(args.report, {"schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "last_run": state["last_run"], "processed": report_records, "pending_candidates": candidates[len(selected) : len(selected) + 50]})
    return state["last_run"]


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("data/automation-state.json"))
    parser.add_argument("--output", type=Path, default=Path("web/generated/articles.json"))
    parser.add_argument("--report", type=Path, default=Path("web/generated/run-report.json"))
    parser.add_argument("--fixture", type=Path, help="Use a saved Europe PMC JSON response for deterministic tests")
    parser.add_argument("--xml-fixture", type=Path, help="Use a saved PMC XML document for deterministic tests")
    parser.add_argument("--query", default=os.environ.get("LITERATURE_QUERY", DEFAULT_QUERY))
    parser.add_argument("--candidate-limit", type=int, default=int(os.environ.get("CANDIDATE_LIMIT", "40")))
    parser.add_argument("--max-new", type=int, default=int(os.environ.get("DAILY_NEW_LIMIT", "5")))
    parser.add_argument("--attempt-limit", type=int, default=int(os.environ.get("ATTEMPT_LIMIT", "0")), help="Maximum candidates inspected in one run; defaults to three times max-new")
    parser.add_argument("--min-score", type=int, default=int(os.environ.get("MIN_RELEVANCE_SCORE", "12")))
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        summary = run(parse_args())
        print(json.dumps(summary, ensure_ascii=False))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"collector failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
