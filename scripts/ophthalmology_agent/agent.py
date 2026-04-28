#!/usr/bin/env python3
"""Ophthalmology Intelligence Agent (optimized)."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

LOGGER = logging.getLogger("ophthalmology_agent")

USER_AGENT = (
    "Mozilla/5.0 (compatible; OphthalmologyIntelligenceAgent/1.1; "
    "+https://example.org/ophthalmology-agent)"
)


@dataclass
class Item:
    title: str
    source: str
    link: str
    published_at: str = ""
    authors: List[str] = field(default_factory=list)
    institutions: List[str] = field(default_factory=list)
    doi: str = ""
    pmid: str = ""
    abstract: str = ""
    raw_payload: Dict = field(default_factory=dict)

    category: str = ""
    evidence_type: str = ""
    key_findings: str = ""
    clinical_relevance: str = ""
    limitations: str = ""
    risk_or_controversy: str = ""
    credibility_score: float = 0.0


def request_text(url: str, timeout: int = 25, retries: int = 2) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_exc = None
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # pragma: no cover
            last_exc = exc
            time.sleep(0.8)
    raise RuntimeError(f"GET failed for {url}: {last_exc}")


def request_json(url: str, timeout: int = 25, retries: int = 2) -> Dict:
    return json.loads(request_text(url, timeout=timeout, retries=retries))


def parse_pubmed(max_rows: int, keyword: str) -> List[Item]:
    q = urllib.parse.quote(f"({keyword}) AND ophthalmology")
    search_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&retmode=json&retmax={max_rows}&sort=pub+date&term={q}"
    )
    id_list = request_json(search_url).get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return []

    summary_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&retmode=json&id={','.join(id_list)}"
    )
    summary = request_json(summary_url).get("result", {})

    out: List[Item] = []
    for pid in id_list:
        row = summary.get(pid, {})
        article_ids = row.get("articleids", [])
        doi = next((x.get("value", "") for x in article_ids if x.get("idtype") == "doi"), "")
        out.append(
            Item(
                title=row.get("title", "").strip(),
                source="PubMed",
                link=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                published_at=str(row.get("pubdate", "")),
                authors=[a.get("name", "") for a in row.get("authors", []) if a.get("name")],
                doi=doi,
                pmid=pid,
                raw_payload=row,
            )
        )
    return [x for x in out if x.title and x.link]


def parse_europe_pmc(max_rows: int, keyword: str) -> List[Item]:
    q = urllib.parse.quote(f"{keyword} ophthalmology")
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json&pageSize={max_rows}&sort_date:y"
    rows = request_json(url).get("resultList", {}).get("result", [])

    out: List[Item] = []
    for row in rows:
        pmid = row.get("pmid", "")
        fulltext = row.get("fullTextUrlList", {}).get("fullTextUrl", [])
        first_link = fulltext[0].get("url") if fulltext else ""
        link = first_link if first_link else (f"https://europepmc.org/article/MED/{pmid}" if pmid else "https://europepmc.org")
        out.append(
            Item(
                title=row.get("title", "").strip(),
                source="Europe PMC",
                link=link,
                published_at=row.get("firstPublicationDate", ""),
                authors=[a.strip() for a in row.get("authorString", "").split(",") if a.strip()][:10],
                doi=row.get("doi", ""),
                pmid=pmid,
                abstract=row.get("abstractText", ""),
                raw_payload=row,
            )
        )
    return [x for x in out if x.title and x.link]


def parse_crossref(max_rows: int, keyword: str) -> List[Item]:
    q = urllib.parse.quote(f"{keyword} ophthalmology")
    url = f"https://api.crossref.org/works?sort=published&order=desc&rows={max_rows}&query.title={q}"
    rows = request_json(url).get("message", {}).get("items", [])

    out: List[Item] = []
    for row in rows:
        doi = row.get("DOI", "")
        title = (row.get("title", [""]) or [""])[0].strip()
        pub_parts = row.get("created", {}).get("date-parts", [[""]])[0]
        pub_date = "-".join(str(x) for x in pub_parts if x != "")
        authors: List[str] = []
        institutions: List[str] = []
        for person in row.get("author", [])[:10]:
            full = " ".join([person.get("given", ""), person.get("family", "")]).strip()
            if full:
                authors.append(full)
            for aff in person.get("affiliation", []):
                name = aff.get("name", "").strip()
                if name:
                    institutions.append(name)

        out.append(
            Item(
                title=title,
                source="Crossref",
                link=f"https://doi.org/{doi}" if doi else "",
                published_at=pub_date,
                authors=authors,
                institutions=list(dict.fromkeys(institutions)),
                doi=doi,
                raw_payload=row,
            )
        )
    return [x for x in out if x.title and x.link]


def parse_rss(name: str, url: str, keywords: List[str]) -> List[Item]:
    xml_text = request_text(url)
    root = ET.fromstring(xml_text)
    rows: List[Item] = []
    for node in root.findall(".//item")[:100]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        published = (node.findtext("pubDate") or node.findtext("dc:date") or "").strip()
        desc = (node.findtext("description") or "").strip()
        if not title or not link:
            continue
        txt = f"{title} {desc}".lower()
        if keywords and not any(kw.lower() in txt for kw in keywords):
            continue
        rows.append(
            Item(title=title, source=name, link=link, published_at=published, abstract=desc, raw_payload={"rss": True})
        )
    return rows


def collect(config: Dict) -> List[Item]:
    src = config["sources"]
    keywords = config.get("keywords", [])
    all_items: List[Item] = []

    for kw in keywords:
        if src.get("pubmed", {}).get("enabled", False):
            try:
                all_items.extend(parse_pubmed(int(src["pubmed"].get("max_results_per_keyword", 5)), kw))
            except Exception as exc:
                LOGGER.warning("PubMed failed [%s]: %s", kw, exc)
        if src.get("europe_pmc", {}).get("enabled", False):
            try:
                all_items.extend(parse_europe_pmc(int(src["europe_pmc"].get("max_results_per_keyword", 5)), kw))
            except Exception as exc:
                LOGGER.warning("Europe PMC failed [%s]: %s", kw, exc)
        if src.get("crossref", {}).get("enabled", False):
            try:
                all_items.extend(parse_crossref(int(src["crossref"].get("max_results_per_keyword", 5)), kw))
            except Exception as exc:
                LOGGER.warning("Crossref failed [%s]: %s", kw, exc)

    rss_sources = {
        "who_news_rss": "WHO",
        "fda_news_rss": "FDA",
        "ema_news_rss": "EMA",
        "nmpa_news_rss": "NMPA",
        "aao_news_rss": "AAO",
        "myopia_profile_rss": "Myopia Profile",
    }
    for key, name in rss_sources.items():
        cfg = src.get(key, {})
        if cfg.get("enabled") and cfg.get("url"):
            try:
                all_items.extend(parse_rss(name, cfg["url"], keywords))
            except Exception as exc:
                LOGGER.warning("RSS failed [%s]: %s", name, exc)

    return all_items


def filter_trusted(items: List[Item], trusted_domains: List[str]) -> List[Item]:
    def trusted(link: str) -> bool:
        host = urllib.parse.urlparse(link).netloc.lower()
        return any(host.endswith(d) for d in trusted_domains)

    return [x for x in items if trusted(x.link)]


def deduplicate(items: List[Item], threshold: float) -> List[Item]:
    unique: List[Item] = []
    seen_ids = set()

    for item in items:
        key = item.doi.lower() or item.pmid or item.link
        if key in seen_ids:
            continue
        seen_ids.add(key)
        unique.append(item)

    final: List[Item] = []
    for item in unique:
        duplicated = False
        for existing in final:
            if SequenceMatcher(None, item.title.lower(), existing.title.lower()).ratio() >= threshold:
                duplicated = True
                break
        if not duplicated:
            final.append(item)
    return final


def classify(item: Item) -> str:
    text = f"{item.title} {item.abstract}".lower()
    if any(k in text for k in ["guideline", "policy", "regulation", "approval", "fda", "ema", "nmpa", "who"]):
        return "policy"
    if any(k in text for k in ["myopia", "red light", "rlrl", "photobiomodulation", "pbm", "trial", "cohort", "randomized"]):
        return "research"
    if any(k in text for k in ["what is", "explainer", "科普", "public awareness"]):
        return "science"
    if any(k in text for k in ["trend", "trending", "hot topic", "surge"]):
        return "trend"
    return "news"


def evidence_type(item: Item) -> str:
    text = f"{item.title} {item.abstract}".lower()
    if "meta-analysis" in text or "systematic review" in text:
        return "系统综述 / meta-analysis"
    if "randomized" in text or "rct" in text:
        return "RCT"
    if "cohort" in text or "observational" in text or "cross-sectional" in text:
        return "观察性研究"
    if "case report" in text:
        return "case report"
    if item.source in {"WHO", "FDA", "EMA", "NMPA", "AAO"}:
        return "guideline / policy"
    return "news / opinion"


def credibility(item: Item) -> float:
    score = 0.2
    if item.doi:
        score += 0.3
    if item.pmid:
        score += 0.25
    if item.authors:
        score += 0.1
    if item.source in {"PubMed", "Europe PMC", "Crossref", "WHO", "FDA", "EMA", "NMPA", "AAO", "Myopia Profile"}:
        score += 0.15
    return round(min(score, 1.0), 2)


def enrich(item: Item) -> None:
    text = re.sub(r"\s+", " ", item.abstract or "").strip()
    if not text:
        text = "摘要缺失，请查看原文链接确认研究设计与终点。"
    item.key_findings = text[:220] + ("..." if len(text) > 220 else "")

    focus = any(k in (item.title + " " + item.abstract).lower() for k in ["myopia", "red light", "rlrl", "photobiomodulation", "pbm"])
    item.clinical_relevance = (
        "与近视防控/红光干预相关，建议关注人群分层、剂量参数与随访周期。" if focus else "与眼科临床相关性中等，建议结合指南判断可转化性。"
    )
    item.limitations = "自动抽取可能遗漏样本量、终点定义、偏倚控制等关键信息。"
    item.risk_or_controversy = "潜在争议：长期安全性和疗效外推边界仍需持续证据。" if focus else "未见显著争议（基于规则识别）。"
    item.credibility_score = credibility(item)


def trend_keywords(items: List[Item], top_n: int = 10) -> List[Tuple[str, int]]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "into", "your", "about", "ophthalmology", "study", "news",
        "眼科", "研究", "临床", "治疗"
    }
    counter: Dict[str, int] = {}
    for item in items:
        words = re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,}", item.title.lower())
        for w in words:
            if w in stop:
                continue
            counter[w] = counter.get(w, 0) + 1
    return sorted(counter.items(), key=lambda x: x[1], reverse=True)[:top_n]


def render_markdown(items: List[Item], report_date: dt.date) -> str:
    by = {"research": [], "policy": [], "science": [], "trend": [], "news": []}
    for i in items:
        by.get(i.category, by["news"]).append(i)

    lines = ["# Ophthalmology Intelligence Brief", f"Date: {report_date.isoformat()}", "", "## Executive Summary"]
    if items:
        for i in items[:5]:
            lines.append(f"- {i.title}（{i.source}，{i.published_at or 'N/A'}）")
    else:
        lines.append("- 本期暂无可用条目。")

    lines += ["", "## 1. Clinical Research", "### RLRL / Red Light Therapy"] + format_items(by["research"])
    lines += ["", "## 2. Policy & Regulation"] + format_items(by["policy"])
    lines += ["", "## 3. Popular Science"] + format_items(by["science"])
    lines += ["", "## 4. Trending Topics"] + format_items(by["trend"])
    lines += ["", "## 5. Watchlist"] + format_items(by["news"])

    lines += ["", "## Hot Keyword Signals"]
    tk = trend_keywords(items)
    if tk:
        for word, cnt in tk:
            lines.append(f"- {word}: {cnt}")
    else:
        lines.append("- 暂无趋势词。")

    return "\n".join(lines) + "\n"


def format_items(items: Iterable[Item]) -> List[str]:
    rows = list(items)
    if not rows:
        return ["- 暂无条目。"]
    out: List[str] = []
    for x in rows:
        out += [
            f"- Title: {x.title}",
            f"  - Source: {x.source}",
            f"  - DOI / PMID: DOI={x.doi or 'N/A'}; PMID={x.pmid or 'N/A'}",
            f"  - Study Type: {x.evidence_type}",
            f"  - Key Findings: {x.key_findings}",
            f"  - Clinical Relevance: {x.clinical_relevance}",
            f"  - Limitations: {x.limitations}",
            f"  - Risk / Controversy: {x.risk_or_controversy}",
            f"  - Credibility Score: {x.credibility_score}",
            f"  - Link: {x.link}",
        ]
    return out


def upload_to_drive(path: Path, cfg: Dict) -> Optional[str]:
    dcfg = cfg.get("google_drive", {})
    if not dcfg.get("enabled"):
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("Drive disabled: missing dependencies: %s", exc)
        return None

    service_account_file = dcfg.get("service_account_file")
    folder_id = dcfg.get("folder_id")
    if not service_account_file or not folder_id:
        LOGGER.warning("Drive upload skipped: empty service account path or folder id")
        return None

    creds = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    service = build("drive", "v3", credentials=creds)
    media = MediaFileUpload(str(path), mimetype="text/markdown")
    body = {"name": path.name, "parents": [folder_id], "mimeType": "text/markdown"}
    result = service.files().create(body=body, media_body=media, fields="id, webViewLink").execute()
    return result.get("webViewLink") or result.get("id")


def run_once(config: Dict) -> Path:
    raw = collect(config)
    LOGGER.info("Collected raw items: %d", len(raw))
    trusted = filter_trusted(raw, config.get("quality", {}).get("trusted_domains", []))
    deduped = deduplicate(trusted, float(config.get("quality", {}).get("title_similarity_threshold", 0.92)))
    LOGGER.info("Trusted+deduped items: %d", len(deduped))

    for x in deduped:
        x.category = classify(x)
        x.evidence_type = evidence_type(x)
        enrich(x)

    today = dt.date.today()
    out_dir = Path(config["output"]["directory"])
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = config["output"].get("filename_prefix", "ophthalmology_intelligence_brief")

    md_path = out_dir / f"{prefix}_{today.isoformat()}.md"
    md_path.write_text(render_markdown(deduped, today), encoding="utf-8")

    raw_path = out_dir / f"{prefix}_{today.isoformat()}_raw.json"
    raw_path.write_text(json.dumps([dataclasses.asdict(x) for x in deduped], ensure_ascii=False, indent=2), encoding="utf-8")

    drive_link = upload_to_drive(md_path, config)
    if drive_link:
        LOGGER.info("Drive uploaded: %s", drive_link)

    return md_path


def sleep_until(schedule: str, time_utc: str, weekday: str) -> None:
    now = dt.datetime.utcnow()
    hh, mm = [int(x) for x in time_utc.split(":", 1)]
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if schedule == "daily":
        if target <= now:
            target += dt.timedelta(days=1)
    elif schedule == "weekly":
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        d = (days.index(weekday.lower()) - now.weekday()) % 7
        if d == 0 and target <= now:
            d = 7
        target += dt.timedelta(days=d)
    else:
        return

    secs = max(1, int((target - now).total_seconds()))
    LOGGER.info("Next run at %s UTC (%s seconds)", target.isoformat(), secs)
    time.sleep(secs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ophthalmology Intelligence Agent")
    parser.add_argument("--config", required=True)
    parser.add_argument("--schedule", choices=["manual", "daily", "weekly"], default=None)
    parser.add_argument("--at", default=None)
    parser.add_argument("--weekday", default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    schedule = args.schedule or cfg.get("schedule", {}).get("frequency", "manual")
    at = args.at or cfg.get("schedule", {}).get("time_utc", "08:00")
    weekday = args.weekday or cfg.get("schedule", {}).get("weekday", "monday")

    if schedule == "manual":
        output = run_once(cfg)
        LOGGER.info("Generated report: %s", output)
        return 0

    while True:
        sleep_until(schedule, at, weekday)
        output = run_once(cfg)
        LOGGER.info("Generated report: %s", output)


if __name__ == "__main__":
    sys.exit(main())
