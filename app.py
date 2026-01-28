# from flask import Flask
# app = Flask(__name__)

# @app.route('/')
# def hello_world():
#     return 'Hello, World!'
import os
S2_API_KEY = os.environ.get('S2_API_KEY')
from typing import Any, Dict, List, Set, Tuple
import re
import requests
from flask import Flask, jsonify, request
import json
import time

app = Flask(__name__)

# Simple CORS header - allow all origins
@app.after_request
def after_request(response):
    #our server isnt blacklisting requests from anywhere
    response.headers.add('Access-Control-Allow-Origin', '*')
    return response

# ---- helpers ----

def s2_headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    if S2_API_KEY:
        headers["x-api-key"] = S2_API_KEY
    return headers

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()

def author_name_matches(paper_authors: List[Dict[str, Any]], first: str, last: str) -> bool:
    first_n = norm(first)
    last_n = norm(last)
    if not first_n and not last_n:
        return True
    for a in paper_authors or []:
        name = norm(a.get("name", ""))
        if first_n and first_n not in name:
            continue
        if last_n and last_n not in name:
            continue
        return True
    return False

def pick_best_match(matches: List[Dict[str, Any]], first: str, last: str) -> Tuple[Dict[str, Any], bool]:
    """
    Returns (chosen_match, used_author_filter)
    If any match has an author that contains first+last, pick the first such match.
    Else pick the first item.
    """
    if not matches:
        raise ValueError("No matches returned")

    for m in matches:
        authors = m.get("authors") or []
        if author_name_matches(authors, first, last):
            return m, True

    return matches[0], False

def extract_author_ids_from_citations_payload(payload: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for item in payload.get("data", []) or []:
        citing = item.get("citingPaper") or item.get("paper") or item
        authors = (citing or {}).get("authors") if isinstance(citing, dict) else None
        if not authors:
            continue
        for a in authors:
            aid = a.get("authorId")
            if aid:
                ids.add(str(aid))
    return ids

# ---- route ----

@app.get("/findPaperCitations")
def find_paper_citations():
    paper_title = request.args.get("paperTitle", "").strip()
    author_first = request.args.get("authorFirstName", "").strip()
    author_last = request.args.get("authorLastName", "").strip()

    if not paper_title:
        return jsonify({
            "error": "Missing required query param: paperTitle",
            "example": "/findPaperCitations?paperTitle=...&authorFirstName=...&authorLastName=..."
        }), 400

    try:
        # 1) Title match search (no matchScore)
        match_url = f"{S2_BASE}/paper/search/match"
        match_params = {
            "query": paper_title,
            "fields": "paperId,title,authors"
        }
        time.sleep(1)
        r = requests.get(match_url, params=match_params, headers=s2_headers(), timeout=DEFAULT_TIMEOUT_SECS)
        print(f"[S2 match] {r.status_code} {r.url} -> {r.text[:1000]}")
        #data looks like:"paperId": "b8b8d5655df1c6a71bbb713387863e34cc055332", 
        # #"title": "Detecting Language Model Attacks with Perplexity",
        # # "authors": [{"authorId": "2083980189", "name": "Gabriel Alon"}
        r.raise_for_status()
        match_json = r.json()

        matches = match_json.get("data") or []
        if not matches:
            return jsonify({"paperTitle": paper_title, "matchesFound": 0, "results": []}), 200

        chosen, used_author_filter = pick_best_match(matches, author_first, author_last)
        paper_id = chosen.get("paperId")
        if not paper_id:
            return jsonify({"error": "Semantic Scholar match response missing paperId"}), 502

        # 2) Fetch citations (GET, not POST) and collect authorIds
        citations_url = f"{S2_BASE}/paper/{paper_id}/citations"
        citations_params = {
            "fields": "citingPaper.authors,citingPaper.title", #current experiment
            "limit": 4 #api limit reached here
        }
        #[{"citingPaper": {"paperId": "c5633cd7829d812ca1e2d316f07d048d726be023",
        # # "title": "TrojanPraise: Jailbreak LLMs via Benign Fine-Tuning", 
        # "authors": [{"authorId": "2321895643", "name": "Zhixin Xie"}
        time.sleep(1)
        rc = requests.get(citations_url, params=citations_params, headers=s2_headers(), timeout=DEFAULT_TIMEOUT_SECS)
        print(f"[S2 citations] {rc.status_code} {rc.url} -> {rc.text[:500]}")
        print("--------")
        rc.raise_for_status()
        citations_json = rc.json()

        author_ids = sorted(list(extract_author_ids_from_citations_payload(citations_json)))
        if not author_ids:
            return jsonify({
                "matchedPaper": {"paperId": paper_id, "title": chosen.get("title")},
                "usedAuthorFilter": used_author_filter,
                "citingAuthors": []
            }), 200

        # 3) Batch author lookup for affiliations
        author_batch_url = f"{S2_BASE}/author/batch"
        author_batch_params = {"fields": "name,affiliations"}
        body = {"ids": author_ids}
        time.sleep(1)
        ra = requests.post(
            author_batch_url,
            params=author_batch_params,
            json=body,
            headers={**s2_headers(), "Content-Type": "application/json"},
            timeout=DEFAULT_TIMEOUT_SECS,
        )
        #data looks like:  {"authorId": "11269472", "name": "I. Masi", "affiliations": []},
        print(f"[S2 author batch] {ra.status_code} {ra.url} -> {ra.text[:500]}")
        #ra.url wont give you the detail for post type request from opening url you'd have to use postman
        ra.raise_for_status()
        authors_json = ra.json()

        citing_authors_out = []
        if isinstance(authors_json, list):
            for a in authors_json:
                citing_authors_out.append({
                    "authorId": a.get("authorId"),
                    "name": a.get("name"),
                    "affiliations": a.get("affiliations") or []
                    #need to append titles list by looking for citations with same author name then add it here
                })
        else:
            citing_authors_out = authors_json

        raw_count = sum(1 for a in citing_authors_out if a.get("affiliations"))
        total_count = len(citing_authors_out)
        pct_with_affiliations = (raw_count / total_count * 100) if total_count else 0.0
        print(f"Authors with non-empty affiliations: {raw_count} ({pct_with_affiliations:.2f}%)")

        # write citing authors to a local JSON file
        out_path = "experimental_data/citing_authors_out.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(citing_authors_out, f, ensure_ascii=False, indent=2)

        return jsonify({
            #python may be reordering it when presenting in the web app when run
            "matchedPaper": {
                "paperId": paper_id,
                "title": chosen.get("title"),
                "matchedByTitleQuery": paper_title,
                "authorProvided": {"first": author_first, "last": author_last},
            },
            "usedAuthorFilter": used_author_filter,
            "citingAuthors": citing_authors_out
        }), 200

    except requests.HTTPError as e:
        resp_text = ""
        try:
            resp_text = e.response.text if e.response is not None else ""
        except Exception:
            pass
        return jsonify({"error": "Upstream Semantic Scholar API error", "details": str(e), "response": resp_text[:2000]}), 502
    except Exception as e:
        return jsonify({"error": "Server error", "details": str(e)}), 500

S2_BASE = "https://api.semanticscholar.org/graph/v1"
DEFAULT_TIMEOUT_SECS = 20

if __name__ == "__main__":
    app.run(debug=True, port=5000)
#example
#    #/findPaperCitations?paperTitle=Detecting Language Model Attacks with Perplexity&authorFirstName=Gabriel&authorLastName=Alon
#http://127.0.0.1:5000/findPaperCitations?paperTitle=Detecting Language Model Attacks with Perplexity&authorFirstName=Gabriel&authorLastName=Alon
