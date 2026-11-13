"""
Scholar MCP server — literature search connector for Claude.

Tools:
  search_openalex          free key (OPENALEX_API_KEY)          openalex.org/settings/api
  search_semantic_scholar  works without key; optional S2_API_KEY
  search_web_of_science    WOS_API_KEY (Clarivate Starter API; free plan = 50 req/day)
  search_google_scholar    SERPAPI_KEY (SerpApi; free plan = 250 searches/month)
  get_paper_by_doi         OpenAlex single-record lookup
  get_bibtex               DOI -> BibTeX via doi.org (no key)

Any tool whose key is missing returns a clear message instead of failing.

Run locally :  python server.py          -> http://localhost:8000/<MCP_PATH>
Env vars    :  PORT (host sets it), MCP_PATH (default /mcp — use a long random
               path in production so only you can reach the server)
"""

import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP("Scholar Search")

TIMEOUT = httpx.Timeout(30.0)
UA = {"User-Agent": "scholar-mcp/1.0"}


def _get(url, params=None, headers=None):
    with httpx.Client(timeout=TIMEOUT, headers={**UA, **(headers or {})},
                      follow_redirects=True) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r


def _err(source, e):
    if isinstance(e, httpx.HTTPStatusError):
        return f"{source} error {e.response.status_code}: {e.response.text[:300]}"
    return f"{source} error: {e}"


def _clip(n, lo=1, hi=25):
    return max(lo, min(hi, int(n)))


# ---------------------------------------------------------------- OpenAlex
def _oa_work(w):
    authors = [a["author"]["display_name"] for a in w.get("authorships", [])[:6]]
    src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
    return {
        "title": w.get("display_name"),
        "year": w.get("publication_year"),
        "authors": authors,
        "journal": src,
        "doi": w.get("doi"),
        "cited_by": w.get("cited_by_count"),
        "open_access_url": (w.get("open_access") or {}).get("oa_url"),
        "openalex_id": w.get("id"),
    }


@mcp.tool
def search_openalex(query: str, limit: int = 10, year_from: int | None = None,
                    year_to: int | None = None, sort_by_citations: bool = False) -> list | str:
    """Search ~250M scholarly works in OpenAlex (good chemistry/physics coverage).
    Returns title, year, authors, journal, DOI, citation count, open-access link."""
    key = os.getenv("OPENALEX_API_KEY")
    if not key:
        return "OPENALEX_API_KEY not set. Get a free key at openalex.org/settings/api."
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    params = {"search": query, "per_page": _clip(limit), "api_key": key}
    if filters:
        params["filter"] = ",".join(filters)
    if sort_by_citations:
        params["sort"] = "cited_by_count:desc"
    try:
        data = _get("https://api.openalex.org/works", params).json()
        return [_oa_work(w) for w in data.get("results", [])]
    except Exception as e:
        return _err("OpenAlex", e)


@mcp.tool
def get_paper_by_doi(doi: str) -> dict | str:
    """Full metadata for one paper by DOI (e.g. 10.1021/jacs.0c01234), incl. abstract."""
    key = os.getenv("OPENALEX_API_KEY")
    doi = doi.replace("https://doi.org/", "").strip()
    params = {"api_key": key} if key else None
    try:
        w = _get(f"https://api.openalex.org/works/doi:{doi}", params).json()
        out = _oa_work(w)
        inv = w.get("abstract_inverted_index")
        if inv:
            pos = {i: word for word, idx in inv.items() for i in idx}
            out["abstract"] = " ".join(pos[i] for i in sorted(pos))
        out["referenced_works_count"] = len(w.get("referenced_works", []))
        return out
    except Exception as e:
        return _err("OpenAlex", e)


# -------------------------------------------------------- Semantic Scholar
@mcp.tool
def search_semantic_scholar(query: str, limit: int = 10,
                            year: str | None = None) -> list | str:
    """Search Semantic Scholar. `year` can be '2020' or a range '2018-2024'.
    Returns title, year, authors, venue, citations, DOI, open PDF link, TLDR."""
    headers = {"x-api-key": os.getenv("S2_API_KEY")} if os.getenv("S2_API_KEY") else {}
    params = {
        "query": query,
        "limit": _clip(limit, hi=100),
        "fields": "title,year,authors,venue,citationCount,externalIds,openAccessPdf,tldr",
    }
    if year:
        params["year"] = year
    try:
        data = _get("https://api.semanticscholar.org/graph/v1/paper/search",
                    params, headers).json()
        out = []
        for p in data.get("data", []):
            out.append({
                "title": p.get("title"),
                "year": p.get("year"),
                "authors": [a["name"] for a in (p.get("authors") or [])[:6]],
                "venue": p.get("venue"),
                "cited_by": p.get("citationCount"),
                "doi": (p.get("externalIds") or {}).get("DOI"),
                "pdf": (p.get("openAccessPdf") or {}).get("url"),
                "tldr": (p.get("tldr") or {}).get("text"),
            })
        return out
    except Exception as e:
        return _err("Semantic Scholar", e)


# ---------------------------------------------------------- Web of Science
@mcp.tool
def search_web_of_science(query: str, limit: int = 10,
                          field: str = "TS") -> list | str:
    """Search Web of Science Core Collection (Clarivate Starter API).
    field: TS=topic (title/abstract/keywords), TI=title, AU=author, DO=DOI, PY=year.
    Advanced: pass a full WoS query in `query` and set field='' (e.g. 'TS=(DFT) AND PY=2024')."""
    key = os.getenv("WOS_API_KEY")
    if not key:
        return "WOS_API_KEY not set. Request a Starter API key at developer.clarivate.com."
    q = f"{field}=({query})" if field else query
    params = {"q": q, "db": "WOS", "limit": _clip(limit, hi=50), "page": 1}
    try:
        data = _get("https://api.clarivate.com/apis/wos-starter/v1/documents",
                    params, {"X-ApiKey": key}).json()
        out = []
        for h in data.get("hits", []):
            src = h.get("source") or {}
            cites = h.get("citations") or []
            out.append({
                "title": h.get("title"),
                "year": src.get("publishYear"),
                "journal": src.get("sourceTitle"),
                "authors": [a.get("displayName") for a in
                            ((h.get("names") or {}).get("authors") or [])[:6]],
                "doi": (h.get("identifiers") or {}).get("doi"),
                "times_cited": cites[0].get("count") if cites else None,
                "wos_id": h.get("uid"),
                "link": (h.get("links") or {}).get("record"),
            })
        return {"total": (data.get("metadata") or {}).get("total"), "results": out}
    except Exception as e:
        return _err("Web of Science", e)


# ---------------------------------------------------------- Google Scholar
@mcp.tool
def search_google_scholar(query: str, limit: int = 10,
                          year_from: int | None = None,
                          year_to: int | None = None) -> list | str:
    """Search Google Scholar (via SerpApi — Google has no official Scholar API).
    Returns title, link, publication summary, snippet, cited-by count."""
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return "SERPAPI_KEY not set. Free key (250 searches/month) at serpapi.com."
    params = {"engine": "google_scholar", "q": query,
              "num": _clip(limit, hi=20), "api_key": key}
    if year_from:
        params["as_ylo"] = year_from
    if year_to:
        params["as_yhi"] = year_to
    try:
        data = _get("https://serpapi.com/search.json", params).json()
        if "error" in data:
            return f"SerpApi: {data['error']}"
        out = []
        for r in data.get("organic_results", []):
            out.append({
                "title": r.get("title"),
                "link": r.get("link"),
                "publication": (r.get("publication_info") or {}).get("summary"),
                "snippet": r.get("snippet"),
                "cited_by": (((r.get("inline_links") or {}).get("cited_by")) or {}).get("total"),
                "pdf": next((x.get("link") for x in r.get("resources", [])
                             if x.get("file_format") == "PDF"), None),
            })
        return out
    except Exception as e:
        return _err("Google Scholar/SerpApi", e)


# ------------------------------------------------------------------ BibTeX
@mcp.tool
def get_bibtex(doi: str) -> str:
    """BibTeX entry for a DOI (via doi.org content negotiation, no key needed)."""
    doi = doi.replace("https://doi.org/", "").strip()
    try:
        return _get(f"https://doi.org/{doi}",
                    headers={"Accept": "application/x-bibtex"}).text
    except Exception as e:
        return _err("doi.org", e)


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        path=os.getenv("MCP_PATH", "/mcp"),
    )
