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
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


SCHEMA_VERSION = 1
DEFAULT_QUERY = (
    'OPEN_ACCESS:Y AND ((aging OR ageing OR senescence OR geroscience OR "age-related" OR '
    'lifespan OR diabetes OR "metabolic syndrome" OR obesity) AND '
    '("long-read" OR "long read" OR "short-read" OR "short read" OR nanopore OR PacBio OR HiFi '
    'OR Illumina OR "third-generation sequencing" OR "second-generation sequencing" OR '
    'metagenom* OR multi-omics OR multiomics OR "single-cell" OR "RNA sequencing" OR RNA-seq '
    'OR WGS OR "whole-genome" OR "spatial transcriptomics"))'
)
EUROPE_PMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EASYSCHOLAR_URL = "https://www.easyscholar.cc/open/getPublicationRank"
NCBI_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_FIGURE_BYTES = 32 * 1024 * 1024
IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
)
# NCBI ESummary does not return the abstract.  A PMC ESearch match therefore
# often has a zero title-only score even though the paper matched the complete
# ageing + sequencing query.  Keep such hits eligible for the later full-text
# and figure gates; those gates are the final relevance/quality check.
NCBI_QUERY_MATCH_SCORE = 12


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


def iter_leaf_sections(node: ET.Element, parents: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], str]]:
    """Yield each leaf XML section once, avoiding nested-section duplication."""
    if local_name(node.tag) != "sec":
        for child in node:
            yield from iter_leaf_sections(child, parents)
        return
    title = element_text(first_child(node, "title"))
    current = parents + ((title,) if title else ())
    child_sections = [child for child in node if local_name(child.tag) == "sec"]
    if child_sections:
        for child in child_sections:
            yield from iter_leaf_sections(child, current)
        return
    text = element_text(node)
    if text:
        yield current, text


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
        "aging": (6, ["aging", "ageing", "senescence", "geroscience", "age-related", "age-associated", "lifespan", "inflammaging", "衰老"]),
        "sequencing": (7, ["long-read", "long read", "nanopore", "pacbio", "hifi", "third-generation sequencing", "metagenom", "rna sequencing", "single-cell"]),
        "multiomics": (4, ["multi-omics", "multiomics", "transcriptom", "methylom", "proteom", "metabolom", "epigenom"]),
        "tools": (3, ["assembler", "benchmark", "pipeline", "bioinformatic", "software", "database", "workflow"]),
        "disease_bridge": (5, ["diabetes", "diabetic", "metabolic syndrome", "metabolic disease", "obesity", "cardiovascular", "kidney disease"]),
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


def candidate_matches_scope(record: Mapping[str, Any]) -> bool:
    """Keep the daily queue inside the requested ageing/omics scope.

    Europe PMC ranks by citation count as well as text relevance.  A highly
    cited sequencing paper can therefore reach the old numeric threshold
    without mentioning ageing at all.  Requiring one biological-topic signal
    and one sequencing/omics signal prevents those papers from consuming the
    five-paper daily quota while still allowing diabetes/metabolic bridges and
    computational-method papers.
    """
    text = f"{record.get('title', '')} {record.get('abstract', '')}".lower()
    topic_terms = (
        "aging", "ageing", "senescence", "geroscience", "age-related", "age-associated", "older adult",
        "lifespan", "inflammaging", "diabetes", "diabetic", "metabolic syndrome", "metabolic disease",
        "obesity", "cardiovascular", "kidney disease", "衰老",
    )
    omics_terms = (
        "sequenc", "long-read", "long read", "nanopore", "pacbio", "hifi",
        "metagenom", "transcriptom", "methylom", "proteom", "metabolom",
        "epigenom", "multi-omics", "multiomics", "single-cell", "scRNA",
        "rna-seq", "rna sequencing", "whole-genome", "illumina", "assembler",
        "benchmark", "bioinformatic", "pipeline", "software", "workflow",
    )
    return any(term in text for term in topic_terms) and any(term.lower() in text for term in omics_terms)


def _http_read(request: urllib.request.Request, *, timeout: int, max_bytes: int) -> bytes:
    """Read a bounded response, retrying transient upstream failures."""
    last_error: Optional[BaseException] = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(max_bytes)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in TRANSIENT_HTTP_STATUS or attempt == 3:
                raise
            retry_after = clean_text(exc.headers.get("Retry-After")) if exc.headers else ""
            try:
                delay = max(1, min(30, int(retry_after))) if retry_after else 2 ** attempt
            except ValueError:
                delay = 2 ** attempt
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    if last_error:
        raise last_error
    raise RuntimeError("HTTP request ended without a response")


def http_json(url: str, *, timeout: int = 30, headers: Optional[Mapping[str, str]] = None) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "paper-plot-library/1.0", **(headers or {})})
    payload = _http_read(request, timeout=timeout, max_bytes=8 * 1024 * 1024)
    return json.loads(payload.decode("utf-8"))


def http_text(url: str, *, timeout: int = 40) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "paper-plot-library/1.0", "Accept": "application/xml,text/xml,text/plain"})
    return _http_read(request, timeout=timeout, max_bytes=20 * 1024 * 1024).decode("utf-8", errors="replace")


def image_suffix(payload: bytes, url: str = "") -> str:
    """Return a safe image suffix after checking the binary magic bytes."""
    for magic, suffix in IMAGE_MAGIC:
        if payload.startswith(magic):
            return suffix
    # WebP files begin with RIFF but also carry WEBP at byte 8.  Keep this
    # explicit so an HTML error page cannot be saved as a published image.
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    if payload.lstrip().startswith(b"<svg"):
        return ".svg"
    return ""


def download_image(url: str, destination_stem: Path) -> dict[str, Any]:
    """Download and validate one original figure into a deterministic path."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "paper-plot-library/1.0",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": "https://pmc.ncbi.nlm.nih.gov/",
        },
    )
    payload = _http_read(request, timeout=60, max_bytes=MAX_FIGURE_BYTES)
    if not payload:
        raise ValueError("empty_image_response")
    if len(payload) >= MAX_FIGURE_BYTES:
        raise ValueError("image_too_large")
    suffix = image_suffix(payload, url)
    if not suffix:
        raise ValueError("response_is_not_a_supported_image")
    destination = destination_stem.with_suffix(suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return {
        "path": destination,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "suffix": suffix,
    }


def cache_figure_assets(figures: list[dict[str, Any]], pmcid: str, assets_dir: Optional[Path], *, url_prefix: str = "generated/assets", asset_map: Optional[Mapping[str, str]] = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Persist original figure binaries and attach stable web-relative URLs.

    ``assets_dir`` is intentionally optional for deterministic unit tests. The
    GitHub workflow always supplies ``web/generated/assets`` so a published
    article never depends on a hot-linked image.
    """
    if not assets_dir:
        return figures, []
    paper_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", clean_text(pmcid) or "paper").strip("-") or "paper"
    errors: list[str] = []
    aws_map = dict(asset_map) if asset_map is not None else pmc_aws_file_map(pmcid)
    for index, figure in enumerate(figures, start=1):
        if not isinstance(figure, dict):
            errors.append(f"fig-{index:02d}:invalid_record")
            continue
        source = clean_text(figure.get("image_url") or figure.get("original_url"))
        if not source:
            errors.append(f"fig-{index:02d}:missing_source_url")
            continue
        source_urls = [source]
        filename = Path(urllib.parse.unquote(urllib.parse.urlsplit(source).path)).name.casefold()
        mapped_source = aws_map.get(filename)
        if mapped_source and mapped_source not in source_urls:
            source_urls.insert(0, mapped_source)
        stem = Path(assets_dir) / paper_key / f"fig-{index:02d}"
        last_error: Optional[BaseException] = None
        for candidate_url in source_urls:
            try:
                result = download_image(candidate_url, stem)
                filename = result["path"].name
                relative_url = f"{url_prefix.rstrip('/')}/{paper_key}/{filename}"
                figure.update({
                    "original_url": candidate_url,
                    "source_url": source,
                    "local_url": relative_url,
                    "asset_path": relative_url,
                    "asset_bytes": result["bytes"],
                    "asset_sha256": result["sha256"],
                    "asset_status": "downloaded",
                    "asset_error": "",
                })
                last_error = None
                break
            except (OSError, urllib.error.URLError, ValueError) as exc:
                last_error = exc
        if last_error is not None:
            figure.update({"original_url": source, "asset_status": "error", "asset_error": type(last_error).__name__})
            errors.append(f"fig-{index:02d}:{type(last_error).__name__}")
    return figures, errors


def local_asset_path(assets_dir: Optional[Path], relative_url: Any) -> Optional[Path]:
    """Resolve a stored web-relative asset URL inside the configured asset root."""
    if not assets_dir:
        return None
    parts = Path(clean_text(relative_url)).parts
    if len(parts) < 2:
        return None
    return Path(assets_dir) / parts[-2] / parts[-1]


def local_asset_exists(assets_dir: Optional[Path], relative_url: Any) -> bool:
    path = local_asset_path(assets_dir, relative_url)
    return bool(path and path.is_file() and path.stat().st_size > 0)


def search_europe_pmc(query: str, *, page_size: int = 40, fixture: Optional[Path] = None) -> list[dict[str, Any]]:
    if fixture:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
    else:
        # Citation sorting repeatedly returns famous historical papers (for
        # example coronavirus or the original HMP paper), which can exhaust
        # the daily queue before newer ageing/omics studies are seen.  The
        # reader is a daily feed, so newest publication date is the correct
        # primary order; citation count remains part of the candidate score.
        sort_order = clean_text(os.environ.get("LITERATURE_SORT")) or "FIRST_PDATE_D desc"
        params = urllib.parse.urlencode({"query": query, "format": "json", "pageSize": page_size, "resultType": "core", "sort": sort_order})
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


def _ncbi_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Add the optional NCBI API key and identifying client information."""
    output = dict(params)
    api_key = clean_text(os.environ.get("NCBI_API_KEY"))
    if api_key:
        output["api_key"] = api_key
    tool = clean_text(os.environ.get("NCBI_TOOL")) or "paper_plot_library"
    email = clean_text(os.environ.get("NCBI_EMAIL"))
    output["tool"] = tool
    if email:
        output["email"] = email
    return output


def ncbi_json(endpoint: str, params: Mapping[str, Any], *, timeout: int = 40) -> Any:
    """Call an NCBI E-utilities JSON endpoint with bounded retry handling."""
    query = urllib.parse.urlencode(_ncbi_params(params), doseq=True)
    return http_json(f"{NCBI_EUTILS}/{endpoint}?{query}", timeout=timeout)


def _ncbi_article_ids(summary: Mapping[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    values = summary.get("articleids") or summary.get("articleidslist") or []
    if isinstance(values, Mapping):
        values = [values]
    for item in values:
        if not isinstance(item, Mapping):
            continue
        id_type = clean_text(item.get("idtype") or item.get("type")).lower()
        value = clean_text(item.get("value") or item.get("id"))
        if id_type and value:
            ids[id_type] = value
    return ids


def _ncbi_summary_to_candidate(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a PMC ESummary record into the project's common candidate shape."""
    uid = clean_text(summary.get("uid"))
    ids = _ncbi_article_ids(summary)
    pmcid = ids.get("pmc") or ids.get("pmcid") or (f"PMC{uid}" if uid else "")
    pmid = ids.get("pubmed") or ids.get("pmid") or ""
    doi = ids.get("doi") or ""
    authors = summary.get("authors") or summary.get("authorlist") or []
    if isinstance(authors, Mapping):
        authors = [authors]
    author_names_list = []
    for author in authors:
        if isinstance(author, Mapping):
            name = clean_text(author.get("name") or author.get("authtype"))
        else:
            name = clean_text(author)
        if name:
            author_names_list.append(name)
    pubdate = clean_text(summary.get("pubdate") or summary.get("sortpubdate"))
    year_match = re.search(r"(?:19|20)\d{2}", pubdate)
    item = {
        "title": clean_text(summary.get("title")),
        "authorString": ", ".join(author_names_list),
        "journalTitle": clean_text(summary.get("fulljournalname") or summary.get("source")),
        "pubYear": year_match.group(0) if year_match else "",
        "doi": doi,
        "pmcid": pmcid,
        "pmid": pmid,
        "abstractText": clean_text(summary.get("abstracttext") or summary.get("abstract")),
        "citedByCount": summary.get("pmcrefcount") or 0,
    }
    normalized = normalize_candidate(item)
    normalized["source"] = "NCBI PMC"
    normalized["relevance_score"], normalized["relevance_evidence"] = score_candidate(normalized)
    return normalized


def search_ncbi_pmc(query: str, *, page_size: int = 40) -> list[dict[str, Any]]:
    """Search the open-access PMC collection when Europe PMC is unavailable.

    ESearch returns PMC identifiers, while ESummary supplies stable metadata
    without downloading full article XML.  Full text is fetched later by
    ``make_record`` only for candidates that pass the normal relevance gate.
    """
    # NCBI does not understand Europe PMC's ``OPEN_ACCESS:Y`` field syntax.
    # Strip that field before adding the NCBI equivalent, otherwise a Europe
    # PMC outage produces a malformed fallback query and an empty queue.
    ncbi_query = re.sub(r"(?i)\bOPEN_ACCESS\s*:\s*Y\b\s*(?:AND\s*)?", "", query).strip()
    ncbi_query = re.sub(r"\(\s*\)", "", ncbi_query)
    # The publication gate requires retrievable XML and original figures, so
    # restricting the fallback to the PMC open-access subset is intentional.
    term = f"({ncbi_query}) AND open access[filter]"
    search_payload = ncbi_json("esearch.fcgi", {
        "db": "pmc",
        "term": term,
        "retmode": "json",
        "retmax": max(1, int(page_size)),
        "sort": "pub date",
    })
    result = search_payload.get("esearchresult", {}) if isinstance(search_payload, Mapping) else {}
    ids = result.get("idlist", []) if isinstance(result, Mapping) else []
    if isinstance(ids, str):
        ids = [ids]
    ids = [clean_text(value) for value in ids if clean_text(value)]
    if not ids:
        return []
    summary_payload = ncbi_json("esummary.fcgi", {
        "db": "pmc",
        "id": ",".join(ids),
        "retmode": "json",
    })
    summary_result = summary_payload.get("result", {}) if isinstance(summary_payload, Mapping) else {}
    summaries: list[Mapping[str, Any]] = []
    if isinstance(summary_result, Mapping):
        ordered_ids = summary_result.get("uids", ids)
        if isinstance(ordered_ids, str):
            ordered_ids = [ordered_ids]
        for uid in ordered_ids:
            item = summary_result.get(str(uid))
            if isinstance(item, Mapping):
                summaries.append(item)
    records = [_ncbi_summary_to_candidate(item) for item in summaries]
    for record in records:
        if record["relevance_score"] < NCBI_QUERY_MATCH_SCORE:
            record["relevance_score"] = NCBI_QUERY_MATCH_SCORE
            record["relevance_evidence"] = list(record.get("relevance_evidence", []))
            record["relevance_evidence"].append(
                "NCBI：完整查询命中（ESummary 未提供摘要，待全文质量门禁复核）"
            )
    return sorted(records, key=lambda x: (x["relevance_score"], x["citation_count"], x["year"]), reverse=True)


def pmc_xml_url(pmcid: str) -> str:
    return f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid)}/fullTextXML"


def ncbi_pmc_xml_url(pmcid: str) -> str:
    params = _ncbi_params({"db": "pmc", "id": pmcid, "retmode": "xml"})
    return f"{NCBI_EUTILS}/efetch.fcgi?{urllib.parse.urlencode(params)}"


def fetch_pmc_xml(pmcid: str, *, prefer_ncbi: bool = False) -> str:
    """Fetch PMC XML using the source that is currently most likely healthy."""
    urls = (
        [(ncbi_pmc_xml_url(pmcid), 60), (pmc_xml_url(pmcid), 40)]
        if prefer_ncbi
        else [(pmc_xml_url(pmcid), 40), (ncbi_pmc_xml_url(pmcid), 60)]
    )
    last_error: Optional[BaseException] = None
    for url, timeout in urls:
        try:
            return http_text(url, timeout=timeout)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("PMC XML request ended without a response")


def figure_url(pmcid: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    # The current PMC site serves figure binaries from this canonical host;
    # the frontend still retains old/new host fallbacks for existing records.
    return f"https://pmc.ncbi.nlm.nih.gov/articles/{urllib.parse.quote(pmcid)}/bin/{urllib.parse.quote(href)}"


PMC_AWS_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com/"


def pmc_aws_file_map(pmcid: str) -> dict[str, str]:
    """Return filename -> public AWS URL for one PMC article package.

    PMC moved its article-dataset distribution to the public
    ``pmc-oa-opendata`` bucket in 2026.  The old ``/articles/<pmcid>/bin``
    paths can now return 404 even when the XML remains available.  Articles
    without redistributable media simply return an empty mapping and are
    rejected by the original-figure gate.
    """
    pmcid = clean_text(pmcid)
    if not pmcid:
        return {}
    prefix = urllib.parse.quote(pmcid, safe="") + "."
    try:
        payload = http_text(f"{PMC_AWS_BUCKET}?list-type=2&prefix={prefix}", timeout=30)
        root = ET.fromstring(payload)
    except (OSError, ET.ParseError, urllib.error.URLError, ValueError):
        return {}
    output: dict[str, str] = {}
    image_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp", ".svg"}
    for node in root.iter():
        if local_name(node.tag) != "Key" or not node.text:
            continue
        key = clean_text(node.text)
        name = Path(urllib.parse.unquote(key)).name
        if Path(name).suffix.lower() not in image_suffixes:
            continue
        output.setdefault(name.casefold(), urllib.parse.urljoin(PMC_AWS_BUCKET, urllib.parse.quote(key, safe="/")))
    return output


def _mapped_figure_url(pmcid: str, href: str, asset_map: Optional[Mapping[str, str]]) -> str:
    name = Path(urllib.parse.unquote(clean_text(href))).name.casefold()
    if asset_map and name in asset_map:
        return asset_map[name]
    return figure_url(pmcid, href)


def extract_fulltext(xml_text: str, pmcid: str, asset_map: Optional[Mapping[str, str]] = None) -> tuple[str, str, str, list[dict[str, str]], list[dict[str, str]]]:
    root = ET.fromstring(xml_text)
    paragraphs: list[str] = []
    methods: list[str] = []
    results: list[str] = []
    figures: list[dict[str, str]] = []
    supplements: list[dict[str, str]] = []
    for path, text in iter_leaf_sections(root):
        heading = " / ".join(path)
        paragraphs.append(f"{heading}: {text}" if heading else text)
        lowered = " ".join(path).lower()
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
            figures.append({"label": label or f"Figure {len(figures) + 1}", "caption": caption, "image_url": _mapped_figure_url(pmcid, href, asset_map)})
    for node in root.iter():
        if local_name(node.tag) != "supplementary-material":
            continue
        label = element_text(first_child(node, "label"))
        href = clean_text(node.attrib.get("{http://www.w3.org/1999/xlink}href") or node.attrib.get("href"))
        for child in node.iter():
            if not href and local_name(child.tag) in {"media", "graphic"}:
                href = clean_text(child.attrib.get("{http://www.w3.org/1999/xlink}href") or child.attrib.get("href"))
        if href:
            supplements.append({"label": label or f"Supplementary file {len(supplements) + 1}", "url": _mapped_figure_url(pmcid, href, asset_map), "description": element_text(first_child(node, "caption"))})
    return "\n\n".join(paragraphs), "\n\n".join(methods), "\n\n".join(results), figures, supplements


def easy_scholar_metrics(journal: str, cache: dict[str, Any]) -> dict[str, str]:
    journal = clean_text(journal)
    empty = {"impact_factor": "", "jcr": "", "jcr_quartile": "", "jcr_q1": False, "status": "unavailable_no_secret", "source": "EasyScholar", "retrieved_at": ""}
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
        data = payload.get("data") if isinstance(payload, Mapping) else {}
        official_rank = data.get("officialRank") if isinstance(data, Mapping) else {}
        ranks = official_rank.get("all") if isinstance(official_rank, Mapping) else {}
        if not isinstance(ranks, Mapping):
            ranks = {}
        sci = clean_text(ranks.get("sci"))
        ssci = clean_text(ranks.get("ssci"))
        jcr = " / ".join(x for x in (f"SCI {sci}" if sci else "", f"SSCI {ssci}" if ssci else "") if x)
        sci_quartile = clean_text(sci)
        # EasyScholar has returned values such as "Q1", "SCI Q1", "1区"
        # and "SCI 1区" across API versions.  Normalize them for the strict
        # publication gate while retaining the original display string.
        quartile_text = sci_quartile.casefold().replace(" ", "")
        is_q1 = bool(re.search(r"(?:^|(?:sci|jcr))q?1(?:$|[^0-9])", quartile_text) or re.search(r"(?:^|(?:sci|jcr))1区(?:$|[^0-9])", quartile_text))
        result = {"impact_factor": clean_text(ranks.get("sciif")), "jcr": jcr, "jcr_quartile": sci_quartile, "jcr_q1": is_q1, "status": "matched" if (ranks.get("sciif") or jcr) else "not_found", "source": "EasyScholar", "retrieved_at": utc_now()}
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


def is_jcr_q1(metrics: Mapping[str, Any]) -> bool:
    """Return True only when EasyScholar confirms an SCI/JCR Q1 journal."""
    if metrics.get("jcr_q1") is True:
        return True
    value = clean_text(metrics.get("jcr_quartile") or metrics.get("jcr")).casefold().replace(" ", "")
    return bool(re.search(r"(?:^|(?:sci|jcr))q?1(?:$|[^0-9])", value) or re.search(r"(?:^|(?:sci|jcr))1区(?:$|[^0-9])", value))


def llm_prompt(record: Mapping[str, Any]) -> str:
    figures = "\n".join(f"{f.get('label')}: {f.get('caption')}" for f in record.get("figures", []))
    return f"""你是严谨的生物医学文献编辑，负责生成接近高质量中文公众号长文的论文精读稿。只能使用给出的摘要、Methods、Results 和图注；不能补造材料中没有的数字、样本、实验或结论。如果正文没有提供某个数值，明确写“正文未报告”，不要猜测。首次出现的缩写写出英文全称，后文使用缩写。方法部分要把正文 Methods 翻译成中文并适当压缩，但保留研究对象、样本量、平台、建库/测序流程、关键软件、阈值、统计方法和数据可得性。结果部分要按正文逻辑解释观察、读图顺序、证据支持和结论边界。只返回一个合法 JSON 对象，不要 Markdown 代码围栏、前言或对象后的解释。所有顶层 prose 字段必须是中文字符串，不能是数组或对象。顶层字段必须完整：title_zh, lead, background, research_question, study_design, methods, results, limitations, takeaways, figure_notes。figure_notes 必须与正文图数量一致；每项包含 label、caption_zh、what_question、how_to_read、panel_reading（数组，每项含 panel、experiment、comparison、result、statistics）、interpretation、evidence、boundary。panel_reading 只能根据图注和正文结果填写；如果某个 panel 的数值或统计量没有在材料中出现，写“正文未报告”。\n\n题目：{record.get('title')}\n作者：{', '.join(record.get('authors', []))}\n期刊：{record.get('journal')}\n摘要：{record.get('abstract')}\n正文方法：{record.get('methods_text', '')[:24000]}\n正文结果：{record.get('results_text', '')[:30000]}\n正文图注：{figures[:16000]}"""


def _llm_content_text(value: Any) -> str:
    """Normalize the two content shapes used by DeepSeek/OpenAI-compatible APIs."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    return ""


def _extract_json_object(content: str) -> dict[str, Any]:
    """Parse JSON even when a model adds Markdown fences or a short preamble.

    ``json.loads`` alone is brittle in production: models occasionally return
    ``Here is the JSON:```json ...``` `` or append one sentence after the
    object.  JSONDecoder.raw_decode lets us find a complete object while still
    respecting quoted braces in captions.  We deliberately do not apply
    JavaScript/eval-style repairs (single quotes, unquoted keys, etc.), because
    those can silently change scientific content.
    """
    text = (content or "").lstrip("\ufeff \t\r\n")
    if not text:
        raise ValueError("llm_empty_content")
    # Fast path for valid JSON and the common fenced response.
    stripped = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    stripped = re.sub(r"\s*```\s*$", "", stripped, flags=re.I).strip()
    decoder = json.JSONDecoder()
    for candidate in (text, stripped):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    # Find the first complete JSON object.  raw_decode handles nested objects,
    # arrays and braces occurring inside quoted strings correctly.
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("llm_json_not_found")


def _normalize_llm_article(article: Mapping[str, Any], figure_count: int) -> dict[str, Any]:
    """Normalize harmless shape differences without inventing article prose."""
    output = dict(article)
    # Some compatible endpoints return arrays for long sections.  Joining is
    # lossless and lets the publication gate inspect the complete translation.
    for field in ("title_zh", "lead", "background", "research_question", "study_design", "methods", "results", "limitations", "takeaways"):
        value = output.get(field)
        if isinstance(value, list):
            output[field] = "\n\n".join(clean_text(item) for item in value if clean_text(item))
    # Accept the common one-key wrapper while preserving the exact figure list.
    notes = output.get("figure_notes")
    if isinstance(notes, Mapping):
        for key in ("items", "figures", "notes", "figure_notes"):
            if isinstance(notes.get(key), list):
                output["figure_notes"] = notes[key]
                break
    notes = output.get("figure_notes")
    if not isinstance(notes, list):
        notes = []
    normalized_notes: list[Any] = []
    aliases = {
        "caption": "caption_zh", "图注": "caption_zh", "question": "what_question", "问题": "what_question",
        "read": "how_to_read", "读图": "how_to_read", "interpret": "interpretation", "解读": "interpretation",
        "证据": "evidence", "边界": "boundary", "panels": "panel_reading", "panel": "panel_reading",
    }
    for raw_note in notes:
        if not isinstance(raw_note, Mapping):
            normalized_notes.append(raw_note)
            continue
        note = dict(raw_note)
        for source, target in aliases.items():
            if target not in note and source in note:
                note[target] = note[source]
        panels = note.get("panel_reading")
        if isinstance(panels, Mapping):
            panels = [panels]
        if isinstance(panels, list):
            fixed_panels: list[Any] = []
            for raw_panel in panels:
                if not isinstance(raw_panel, Mapping):
                    fixed_panels.append(raw_panel)
                    continue
                panel = dict(raw_panel)
                panel_aliases = {"面板": "panel", "实验": "experiment", "比较": "comparison", "结果": "result", "统计": "statistics"}
                for source, target in panel_aliases.items():
                    if target not in panel and source in panel:
                        panel[target] = panel[source]
                fixed_panels.append(panel)
            note["panel_reading"] = fixed_panels
        normalized_notes.append(note)
    output["figure_notes"] = normalized_notes
    return output


def generate_llm_article(record: dict[str, Any], feedback: Optional[Iterable[str]] = None) -> tuple[dict[str, Any], str]:
    api_key = clean_text(os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
    if not api_key:
        return {}, "missing_llm_key"
    endpoint = clean_text(os.environ.get("LLM_API_URL")) or "https://api.deepseek.com/chat/completions"
    figure_count = len(record.get("figures", [])) if isinstance(record.get("figures"), list) else 0
    base_prompt = llm_prompt(record) + f"\n本次正文原图数量为 {figure_count}；figure_notes 必须恰好输出 {figure_count} 项，并按原图顺序对应。"
    feedback_text = "; ".join(clean_text(item) for item in (feedback or []) if clean_text(item))
    try:
        max_attempts = max(1, min(3, int(os.environ.get("LLM_MAX_ATTEMPTS", "3"))))
    except (TypeError, ValueError):
        max_attempts = 3
    try:
        max_tokens = max(1024, min(32768, int(os.environ.get("LLM_MAX_TOKENS", "8192"))))
    except (TypeError, ValueError):
        max_tokens = 8192
    json_mode = os.environ.get("LLM_JSON_MODE", "1").casefold() not in {"0", "false", "no"}
    last_error = "llm_error:unknown"
    for attempt in range(max_attempts):
        prompt = base_prompt
        if attempt:
            prompt += "\n上一次输出无法通过结构化校验。请重新生成，必须只输出一个完整 JSON 对象；不要 Markdown 围栏、不要说明文字、不要省略任何 figure_notes。为避免长文截断，每个顶层段落保持 2–5 个完整句子，每个 panel 的说明保持 1–2 个完整句子，但不得省略实验、比较、结果和统计字段。"
            if feedback_text:
                prompt += f"\n上一版还需要修复：{feedback_text}"
        body_data = {
            "model": os.environ.get("LLM_MODEL", "deepseek-chat"),
            "temperature": 0.15,
            # A bounded value prevents the API from silently truncating long
            # multi-figure papers.  It remains configurable for compatible
            # endpoints with a different maximum.
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": "You produce source-grounded scientific Chinese article JSON."},
                {"role": "user", "content": prompt},
            ],
        }
        # DeepSeek supports JSON mode.  Custom OpenAI-compatible endpoints may
        # reject the field, so it can be disabled explicitly.
        if json_mode:
            body_data["response_format"] = {"type": "json_object"}
        body = json.dumps(body_data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "paper-plot-library/1.0"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
            content = _llm_content_text(payload["choices"][0]["message"]["content"])
            article = _normalize_llm_article(_extract_json_object(content), figure_count)
            required = ("title_zh", "lead", "background", "research_question", "study_design", "methods", "results", "limitations", "takeaways", "figure_notes")
            if not all(article.get(field) for field in required):
                last_error = "llm_schema_incomplete"
                continue
            if not isinstance(article.get("figure_notes"), list) or len(article["figure_notes"]) != figure_count:
                last_error = "llm_figure_notes_incomplete"
                continue
            return article, "success" if attempt == 0 else "success_retried"
        except urllib.error.HTTPError as exc:
            last_error = f"llm_http_{exc.code}"
            # A few OpenAI-compatible gateways do not implement DeepSeek's
            # JSON-mode parameter.  Retry once without that optional field;
            # authentication and quota failures still remain visible.
            if exc.code == 400 and json_mode:
                json_mode = False
                continue
            if exc.code not in TRANSIENT_HTTP_STATUS and attempt == max_attempts - 1:
                break
        except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            last_error = f"llm_error:{type(exc).__name__}"
        if attempt < max_attempts - 1:
            time.sleep(min(8, 2 ** attempt))
    return {}, last_error


def publication_gate(record: Mapping[str, Any], article: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    figures = record.get("figures", [])
    if not isinstance(figures, list):
        figures = []
    if not is_jcr_q1(record.get("metrics", {}) if isinstance(record.get("metrics"), Mapping) else {}):
        reasons.append("期刊未确认属于 JCR/SCI 1 区")
    if not record.get("fulltext_ok"):
        reasons.append("正文全文未成功提取")
    if len(figures) == 0:
        reasons.append("没有可追溯的正文原图")
    if any(not isinstance(fig, Mapping) or not clean_text(fig.get("image_url") or fig.get("original_url")) for fig in figures):
        reasons.append("存在缺失正文原图直链")
    if record.get("assets_required") and any(not isinstance(fig, Mapping) or not clean_text(fig.get("local_url") or fig.get("asset_path")) or fig.get("asset_status") != "downloaded" for fig in figures):
        reasons.append("存在未成功缓存到网站的正文原图")
    if any(not isinstance(fig, Mapping) or not clean_text(fig.get("caption")) for fig in figures):
        reasons.append("存在缺失图注")
    if not clean_text(record.get("methods_text")):
        reasons.append("正文 Methods 未提取")
    if not clean_text(record.get("results_text")):
        reasons.append("正文 Results 未提取")
    prose_fields = ("title_zh", "lead", "background", "research_question", "study_design", "methods", "results", "limitations", "takeaways")
    for field in prose_fields:
        if not article.get(field):
            reasons.append(f"AI 字段缺失：{field}")
        elif not isinstance(article.get(field), str):
            reasons.append(f"AI 字段类型错误：{field}")
    if not article.get("figure_notes"):
        reasons.append("AI 字段缺失：figure_notes")
    # The production workflow asks for a full公众号-style article. Keep the
    # fixture/legacy path permissive, but do not publish a production record
    # when the model has returned only one-line placeholders.
    if record.get("assets_required"):
        minimum_lengths = {
            "lead": 40,
            "background": 120,
            "research_question": 60,
            "study_design": 100,
            "methods": 220,
            "results": 220,
            "limitations": 60,
            "takeaways": 60,
        }
        for field, minimum in minimum_lengths.items():
            value = article.get(field)
            if not isinstance(value, str) or len(clean_text(value)) < minimum:
                reasons.append(f"AI 内容过短：{field}")
    notes = article.get("figure_notes")
    if not isinstance(notes, list):
        reasons.append("AI 图组解读不是数组")
    elif len(notes) != len(figures):
        reasons.append("AI 图组解读数量与正文原图数量不一致")
    elif record.get("assets_required"):
        for index, note in enumerate(notes):
            required_note_fields = ("caption_zh", "what_question", "how_to_read", "interpretation", "evidence", "boundary")
            if not isinstance(note, Mapping) or any(not clean_text(note.get(field)) for field in required_note_fields):
                reasons.append(f"第 {index + 1} 张图的图注/读图/证据字段不完整")
                continue
            if len(clean_text(note.get("caption_zh"))) < 12:
                reasons.append(f"第 {index + 1} 张图的中文图注过短")
            if len(clean_text(note.get("how_to_read"))) < 25:
                reasons.append(f"第 {index + 1} 张图的读图说明过短")
            # A caption can be short when the paper reports only a compact
            # benchmark panel.  Require a real interpretation, but do not
            # discard an otherwise complete article because it is below the
            # old 70-character heuristic.
            if len(clean_text(note.get("interpretation"))) < 40:
                reasons.append(f"第 {index + 1} 张图的结果解读过短")
            panels = note.get("panel_reading")
            if not isinstance(panels, list) or not panels:
                reasons.append(f"第 {index + 1} 张图缺少 panel 逐项阅读")
            elif any(not isinstance(panel, Mapping) or any(not clean_text(panel.get(field)) for field in ("panel", "experiment", "comparison", "result", "statistics")) for panel in panels):
                reasons.append(f"第 {index + 1} 张图的 panel 阅读字段不完整")
    elif isinstance(notes, list):
        for index, note in enumerate(notes):
            if not isinstance(note, Mapping) or not clean_text(note.get("caption_zh")) or not clean_text(note.get("interpretation")):
                reasons.append(f"第 {index + 1} 张图的中文图注或解读不完整")
    return not reasons, reasons


def make_record(candidate: dict[str, Any], metrics_cache: dict[str, Any], xml_fixture: Optional[Path] = None, assets_dir: Optional[Path] = None, assets_url_prefix: str = "generated/assets") -> dict[str, Any]:
    record = dict(candidate)
    xml_text = ""
    try:
        if xml_fixture:
            xml_text = xml_fixture.read_text(encoding="utf-8")
        elif record.get("pmcid"):
            xml_text = fetch_pmc_xml(record["pmcid"], prefer_ncbi=record.get("source") == "NCBI PMC")
        if xml_text and record.get("pmcid"):
            # Resolve figure filenames against the supported PMC AWS article
            # dataset before caching.  The legacy PMC ``/bin`` URL is kept as
            # a diagnostic fallback, but it is no longer reliable after the
            # 2026 PMC distribution migration.
            asset_map = {} if xml_fixture else pmc_aws_file_map(record["pmcid"])
            fulltext, methods, results, figures, supplements = extract_fulltext(xml_text, record["pmcid"], asset_map)
            figures, asset_errors = cache_figure_assets(figures, record["pmcid"], assets_dir, url_prefix=assets_url_prefix, asset_map=asset_map)
            record.update({"fulltext_text": fulltext, "methods_text": methods, "results_text": results or fulltext, "figures": figures, "supplements": supplements, "fulltext_ok": bool(fulltext)})
            record["asset_errors"] = asset_errors
        else:
            record.update({"fulltext_text": "", "methods_text": "", "results_text": "", "figures": [], "supplements": [], "fulltext_ok": False, "asset_errors": []})
    except (OSError, ET.ParseError, urllib.error.URLError, ValueError):
        record.update({"fulltext_text": "", "methods_text": "", "results_text": "", "figures": [], "supplements": [], "fulltext_ok": False, "asset_errors": ["fulltext_or_asset_fetch_failed"]})
    record["assets_required"] = bool(assets_dir)
    record["metrics"] = easy_scholar_metrics(record.get("journal", ""), metrics_cache)
    record["journal_display"] = f"{record.get('journal', '')}（{metric_label(record['metrics'])}）"
    record["updated_at"] = utc_now()
    # Do not spend an LLM request on records that cannot provide the source
    # material required by the publication gate.  This is especially important
    # for Europe PMC hits that have a DOI/abstract but no PMCID.
    if not is_jcr_q1(record["metrics"]):
        # Do not spend a DeepSeek request on a journal that cannot be shown
        # under the user's JCR-1-only policy.
        article, ai_status = {}, "skipped_non_jcr_q1"
    elif record.get("fulltext_ok") and record.get("figures") and (not record.get("assets_required") or not record.get("asset_errors")):
        article, ai_status = generate_llm_article(record)
    else:
        article, ai_status = {}, "skipped_no_retrievable_fulltext_or_assets"
    record["article"] = article
    record["ai_status"] = ai_status
    record["publishable"], record["gate_reasons"] = publication_gate(record, article)
    # A syntactically valid draft can still miss one detailed figure field.
    # Give the model one bounded, feedback-directed repair before discarding a
    # paper; source/full-text/JCR failures are never bypassed by this path.
    quality_reasons = [reason for reason in record["gate_reasons"] if reason.startswith("AI ") or reason.startswith("第 ")]
    if (
        record.get("assets_required")
        and ai_status.startswith("success")
        and not record["publishable"]
        and quality_reasons
        and os.environ.get("LLM_GATE_REPAIR", "1").casefold() not in {"0", "false", "no"}
    ):
        repaired, repaired_status = generate_llm_article(record, feedback=quality_reasons)
        if repaired_status.startswith("success"):
            article = repaired
            record["article"] = article
            record["ai_status"] = f"{repaired_status}_gate_repair"
            record["publishable"], record["gate_reasons"] = publication_gate(record, article)
    source_incomplete = ai_status in {"skipped_non_jcr_q1", "skipped_no_retrievable_fulltext_or_assets"}
    record["status"] = "published" if record["publishable"] else ("manual_review" if record["fulltext_ok"] and record["figures"] and not source_incomplete else "discarded")
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


def is_preservable_record(record: Mapping[str, Any], assets_dir: Optional[Path] = None) -> bool:
    """Keep only already-published records that still satisfy the public gate.

    Older state files can contain records created before the original-figure
    and JCR-Q1 rules were added.  They must not silently reappear on the next
    run just because the upstream search is temporarily unavailable.
    """
    metrics = record.get("metrics", {}) if isinstance(record.get("metrics"), Mapping) else {}
    figures = record.get("figures", [])
    if not is_jcr_q1(metrics) or record.get("publishable") is False:
        return False
    if not record.get("fulltext_ok") or not isinstance(figures, list) or not figures:
        return False
    if any(not isinstance(figure, Mapping) or not clean_text(figure.get("caption")) or not clean_text(figure.get("image_url") or figure.get("original_url")) for figure in figures):
        return False
    if record.get("assets_required") and any(not isinstance(figure, Mapping) or figure.get("asset_status") != "downloaded" or not clean_text(figure.get("local_url") or figure.get("asset_path")) for figure in figures):
        return False
    if assets_dir and record.get("assets_required"):
        for figure in figures:
            relative = clean_text(figure.get("local_url") or figure.get("asset_path"))
            if not local_asset_exists(assets_dir, relative):
                return False
    return True


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = load_json(args.state, {"schema_version": SCHEMA_VERSION, "records": {}, "attempted": {}, "metrics_cache": {}})
    records: dict[str, Any] = {}
    raw_records = state.get("records", {}) if isinstance(state.get("records"), Mapping) else {}
    for key, raw_value in raw_records.items():
        if not isinstance(raw_value, Mapping):
            continue
        value = dict(raw_value)
        # Migrate previously published records from remote-only figure URLs to
        # local web assets.  A record is preserved only after every figure has
        # been downloaded and validated; a broken image is never silently kept.
        if args.assets_dir and value.get("fulltext_ok") and isinstance(value.get("figures"), list):
            figures = [dict(item) for item in value["figures"] if isinstance(item, Mapping)]
            if figures and any(
                not clean_text(item.get("local_url") or item.get("asset_path"))
                or not local_asset_exists(args.assets_dir, item.get("local_url") or item.get("asset_path"))
                for item in figures
            ):
                figures, asset_errors = cache_figure_assets(figures, clean_text(value.get("pmcid")) or key, args.assets_dir, url_prefix=args.assets_url_prefix)
                value["figures"] = figures
                value["asset_errors"] = asset_errors
                value["assets_required"] = True
        if is_preservable_record(value, args.assets_dir):
            records[key] = public_projection(value)
    attempted: dict[str, Any] = state.get("attempted", {}) if isinstance(state.get("attempted"), Mapping) else {}
    metrics_cache: dict[str, Any] = state.get("metrics_cache", {}) if isinstance(state.get("metrics_cache"), Mapping) else {}
    # Europe PMC occasionally returns a transient 5xx/429 response even when
    # the article library and the workflow runner are healthy.  A retry is
    # handled in ``_http_read``; if the upstream still cannot be reached,
    # preserve the current library and publish a diagnostic report instead of
    # failing the whole GitHub Actions job.  This makes a missed collection
    # day visible without deleting existing articles or replacing the output
    # with an empty result.
    search_error = ""
    search_status = "success"
    try:
        candidates = search_europe_pmc(args.query, page_size=args.candidate_limit, fixture=args.fixture)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        code = getattr(exc, "code", "")
        detail = clean_text(str(exc))
        search_error = f"Europe PMC HTTP {code}: {detail}" if code else (f"Europe PMC: {detail}" if detail else type(exc).__name__)
        try:
            candidates = search_ncbi_pmc(args.query, page_size=args.candidate_limit)
            search_status = "fallback_ncbi"
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError) as fallback_exc:
            search_status = "upstream_unavailable"
            fallback_code = getattr(fallback_exc, "code", "")
            fallback_detail = clean_text(str(fallback_exc))
            fallback_message = f"NCBI HTTP {fallback_code}: {fallback_detail}" if fallback_code else (f"NCBI: {fallback_detail}" if fallback_detail else type(fallback_exc).__name__)
            search_error = f"{search_error}; {fallback_message}"
            candidates = []
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
    scope_rejected = sum(1 for candidate in eligible_candidates if not candidate_matches_scope(candidate))
    eligible_candidates = [candidate for candidate in eligible_candidates if candidate_matches_scope(candidate)]
    # A PMCID is required for the XML/full-text path used by this project.
    # DOI-only records are kept out of the daily processing quota so that one
    # inaccessible paper cannot consume the whole run.
    candidates = [candidate for candidate in eligible_candidates if clean_text(candidate.get("pmcid"))]
    skipped_without_pmcid = len(eligible_candidates) - len(candidates)
    q1_rejected = 0
    # In the production workflow, EasyScholar is the authoritative Q1 gate.
    # Apply it before selecting the daily queue so Q2/unclassified papers do
    # not consume the five inspection slots.  Fixture/legacy runs remain
    # permissive and are covered by the deterministic tests.
    if args.assets_dir:
        q1_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            metrics = easy_scholar_metrics(clean_text(candidate.get("journal")), metrics_cache)
            if is_jcr_q1(metrics):
                q1_candidates.append(candidate)
            else:
                q1_rejected += 1
        candidates = q1_candidates
    attempt_limit = args.attempt_limit or max(args.max_new * 3, args.max_new)
    selected: list[dict[str, Any]] = []
    processed: list[dict[str, Any]] = []
    published_this_run = 0
    for candidate in candidates[:attempt_limit]:
        if published_this_run >= args.max_new:
            break
        selected.append(candidate)
        record = make_record(candidate, metrics_cache, args.xml_fixture, args.assets_dir, args.assets_url_prefix)
        processed.append(record)
        previous_attempts = int(attempted.get(record["id"], {}).get("attempts", 0)) if isinstance(attempted.get(record["id"]), Mapping) else 0
        attempted[record["id"]] = {"status": record["status"], "attempts": previous_attempts + 1, "updated_at": record["updated_at"], "gate_reasons": record["gate_reasons"]}
        if record["publishable"]:
            records[record["id"]] = public_projection(record)
            published_this_run += 1
    published = sorted(records.values(), key=lambda r: (r.get("year", ""), r.get("updated_at", "")), reverse=True)
    state.update({"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "records": records, "attempted": attempted, "metrics_cache": metrics_cache, "last_run": {"status": search_status, "selected": len(selected), "published": published_this_run, "candidates": len(candidates), "scope_rejected": scope_rejected, "q1_rejected": q1_rejected, "skipped_without_pmcid": skipped_without_pmcid, "search_error": search_error, "preserved_articles": len(records), "asset_dir": str(args.assets_dir) if args.assets_dir else "", "finished_at": utc_now()}})
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
    parser.add_argument("--assets-dir", type=Path, default=None, help="Persist validated original figure binaries here")
    parser.add_argument("--assets-url-prefix", default="generated/assets", help="Web-relative prefix stored in figure.local_url")
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
