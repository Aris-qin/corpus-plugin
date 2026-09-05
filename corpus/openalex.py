"""
corpus.openalex — 查期刊 IF / JCR quartile / CAS zone

OpenAlex API:
  GET https://api.openalex.org/works/{pmid}        # 通过 PMID 反查 venue
  GET https://api.openalex.org/venues/{venue_id}   # 查 venue 的 IF/summary
  GET https://api.openalex.org/journals/{issn_l}   # 按 ISSN 查

策略:
  PMID → work → primary_location.source.issn_l → journals/issn_l → summary.metrics
  IF 字段: summary.2yr_mean_citedness (近似 IF，但跟 JCR IF 不完全一致)
  JCR quartile / CAS zone: OpenAlex 不直接提供，需 JCR/CAS 表（暂 unknown）

OpenAlex 无需 API key，限速 10 req/s（polite pool 需要 email）。
"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional


OPENALEX_BASE = "https://api.openalex.org"


def _http_get_json(url: str, timeout: int = 30) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "openclaw-corpus/0.1 (mailto:l@l.local)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[openalex] GET {url}: {e}")
        return None


def fetch_work_by_pmid(pmid: str) -> Optional[dict]:
    """用 PMID 反查 OpenAlex work（filter=pmid:...）"""
    pmid = pmid.strip()
    url = f"{OPENALEX_BASE}/works/pmid:{urllib.parse.quote(pmid)}"
    data = _http_get_json(url)
    if data is None:
        # 退化路径: search
        url2 = f"{OPENALEX_BASE}/works?filter=ids.openalex:none,pmid:{urllib.parse.quote(pmid)}&per_page=1"
        data = _http_get_json(url2)
        if data and data.get("results"):
            data = data["results"][0]
    return data


def lookup_venue_by_pmid(pmid: str) -> dict:
    """
    通过 PMID 拿 venue 信息。
    返回 dict 兼容 db.upsert_venue 字段:
      {journal, year, impact_factor, jcr_quartile, cas_zone, source}
    失败时全部字段返回 None / 'unknown'。
    """
    result = {
        "journal": None,
        "year": None,
        "impact_factor": None,
        "jcr_quartile": "unknown",
        "cas_zone": "unknown",
        "source": "openalex",
    }
    work = fetch_work_by_pmid(pmid)
    if not work:
        result["source"] = "unknown"
        return result

    # Journal title
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    result["journal"] = source.get("display_name")

    # Year
    pub_date = work.get("publication_date") or ""
    if pub_date and len(pub_date) >= 4:
        try:
            result["year"] = int(pub_date[:4])
        except ValueError:
            pass

    # 2-year mean citedness ≈ IF
    summary = source.get("summary") or {}
    ifc = summary.get("2yr_mean_citedness")
    if ifc is not None:
        try:
            result["impact_factor"] = float(ifc)
        except (TypeError, ValueError):
            pass

    # JCR quartile / CAS zone - OpenAlex 不提供
    # 后续可以接 JCR/CAS 表做映射；MVP 留 unknown
    return result


def lookup_venue_by_issn(issn_l: str) -> Optional[dict]:
    """通过 ISSN-L 查 venue 元信息（含 2yr mean citedness）"""
    url = f"{OPENALEX_BASE}/journals/{urllib.parse.quote(issn_l)}"
    return _http_get_json(url)


# ---------- 自测 ----------

if __name__ == "__main__":
    import sys
    pmid = sys.argv[1] if len(sys.argv) > 1 else "33875643"
    print(f"Looking up PMID {pmid} ...")
    info = lookup_venue_by_pmid(pmid)
    print(json.dumps(info, indent=2, ensure_ascii=False))