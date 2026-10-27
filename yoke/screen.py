"""Decision-model screen module (PLAN M37).

Screens search sources and research claims through router.decide, with deterministic
heuristics, threshold overrides, Ollama-down short circuit, max_items cap, synthetic
factcheck generation, and merge helpers. Screening is advisory.
"""
import copy
import urllib.parse

from . import config, router

DEFAULT_SCREEN = {
    "enabled": True,
    "skip_factcheck": True,
    "escalate_on_unsure": True,
    "max_items": 25,
}

QUESTIONS = {
    "source_screen": {
        "v": {
            "type": "choice",
            "instructions": "Should this search result be kept as a source for the topic?",
            "criteria": {
                "keep": "excerpt is on topic and from a credible, non-spam page",
                "discard": "off topic, spam, SEO filler, or the excerpt does not support the title/relevance",
            },
        }
    },
    "claim_screen": {
        "v": {
            "type": "choice",
            "instructions": "Do the cited excerpts support this claim?",
            "criteria": {
                "supported": "the cited excerpts state this",
                "unsupported": "the excerpts do not state this or contradict it",
            },
        }
    },
}

parse = lambda a: (a["v"]["choice"], a["v"]["confidence"])


def enabled(cfg):
    """cfg.get('research',{}).get('screen',{}).get('enabled', False) truthiness; missing block -> False."""
    if not isinstance(cfg, dict):
        return False
    res = cfg.get("research")
    if not isinstance(res, dict):
        return False
    sc = res.get("screen")
    if not isinstance(sc, dict):
        return False
    return bool(sc.get("enabled", False))


def norm_url(url):
    """Normalize URL: strip whitespace, lowercase scheme and host, drop fragment, strip trailing slash from path."""
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        p = urllib.parse.urlsplit(url.strip())
        scheme = p.scheme.lower()
        netloc = p.netloc.lower()
        path = p.path.rstrip("/")
        return urllib.parse.urlunsplit((scheme, netloc, path, p.query, ""))
    except ValueError:
        return ""


def _cfg(cfg):
    """Return screen config block merged with defaults."""
    base = copy.deepcopy(DEFAULT_SCREEN)
    if isinstance(cfg, dict):
        res = cfg.get("research")
        if isinstance(res, dict):
            sc = res.get("screen")
            if isinstance(sc, dict):
                return config.deep_merge(base, sc)
    return base


def _override(cfg, point, key):
    """Return a shallow copy of cfg with copied router and router['thresholds']
    overriding thresholds[point] from min_confidence[key] if present, else cfg unchanged.
    """
    sc = _cfg(cfg)
    min_conf = sc.get("min_confidence")
    if isinstance(min_conf, dict) and key in min_conf and min_conf[key] is not None:
        cfg_copy = dict(cfg) if isinstance(cfg, dict) else {}
        router_copy = dict(cfg_copy.get("router") or {})
        thresholds_copy = dict(router_copy.get("thresholds") or {})
        thresholds_copy[point] = min_conf[key]
        router_copy["thresholds"] = thresholds_copy
        cfg_copy["router"] = router_copy
        return cfg_copy
    return cfg


def _dedupe_and_filter(sources):
    """Filter sources by heuristics: valid http(s) URL, no duplicates, non-empty excerpt.
    Returns (candidates, discarded).
    """
    candidates = []
    discarded = []
    seen = set()
    for s in (sources or []):
        url = s.get("url", "") if isinstance(s, dict) else ""
        norm = norm_url(url)
        p = urllib.parse.urlsplit(norm)
        if p.scheme not in ("http", "https"):
            discarded.append({"url": url, "reason": "not an http(s) URL", "by": "heuristic"})
            continue
        if norm in seen:
            discarded.append({"url": url, "reason": "duplicate URL", "by": "heuristic"})
            continue
        excerpt = s.get("excerpt", "") if isinstance(s, dict) else ""
        if not excerpt or not excerpt.strip():
            discarded.append({"url": url, "reason": "empty excerpt", "by": "heuristic"})
            continue
        seen.add(norm)
        candidates.append(s)
    return candidates, discarded


def _state_source(topic, src):
    """Build model state string for source screening: intact header with tail-cut excerpt.
    Web excerpts are untrusted input.
    """
    excerpt = (src.get("excerpt") or "") if isinstance(src, dict) else ""
    reserve_excerpt = min(len(excerpt), 500)
    max_header = max(100, router.STATE_CHARS - reserve_excerpt)

    header_parts = []
    if topic:
        header_parts.append("Topic: %s" % topic)
    if isinstance(src, dict):
        if src.get("url"):
            header_parts.append("URL: %s" % src.get("url"))
        if src.get("title"):
            header_parts.append("Title: %s" % src.get("title"))
        if src.get("relevance"):
            header_parts.append("Relevance: %s" % src.get("relevance"))
    header_parts.append("Excerpt: ")
    header = "\n".join(header_parts)

    if len(header) > max_header:
        over = len(header) - max_header
        if topic and len(topic) > over:
            truncated_topic = topic[:max(0, len(topic) - over)]
            header_parts[0] = "Topic: %s" % truncated_topic
            header = "\n".join(header_parts)
        if len(header) > max_header:
            header = header[:max_header]

    budget = max(0, router.STATE_CHARS - len(header))
    tail = excerpt[-budget:] if budget > 0 else ""
    return (header + tail)[:router.STATE_CHARS]


def _state_claim(claim, excerpts):
    """Build model state string for claim screening: intact header with tail-cut excerpt.
    Web excerpts are untrusted input.
    """
    if isinstance(claim, dict):
        claim_text = claim.get("claim", "")
        claim_urls = claim.get("source_urls", [])
    else:
        claim_text = str(claim)
        claim_urls = []

    urls = []
    text_parts = []
    if isinstance(excerpts, str):
        text_parts.append(excerpts)
    elif isinstance(excerpts, list):
        for it in excerpts:
            if isinstance(it, dict):
                if it.get("url"):
                    urls.append(it["url"])
                if it.get("excerpt"):
                    text_parts.append(it["excerpt"])
            elif isinstance(it, (tuple, list)) and len(it) == 2:
                urls.append(it[0])
                text_parts.append(it[1])
            elif isinstance(it, str):
                text_parts.append(it)
    else:
        text_parts.append(str(excerpts))

    if not urls and claim_urls:
        urls = claim_urls

    combined_excerpt = "\n\n".join(t for t in text_parts if t)
    reserve_excerpt = min(len(combined_excerpt), 500)
    max_header = max(100, router.STATE_CHARS - reserve_excerpt)

    header_parts = ["Claim: %s" % claim_text]
    for u in urls:
        header_parts.append("URL: %s" % u)
    header_parts.append("Excerpt: ")
    header = "\n".join(header_parts)

    if len(header) > max_header:
        header = header[:max_header]

    budget = max(0, router.STATE_CHARS - len(header))
    tail = combined_excerpt[-budget:] if budget > 0 else ""
    return (header + tail)[:router.STATE_CHARS]


def _ask(run, a2, a3, a4=None):
    """Wrap router.decide; returns (choice_or_None, conf, reason).
    Accepts either _ask(run, point, state) or _ask(run, cfg_override, point, state).
    """
    if isinstance(a2, dict) or a4 is not None:
        cfg_override, point, state = a2, a3, a4
    else:
        point, state = a2, a3
        cfg = getattr(run, "cfg", {})
        key = "sources" if point == "source_screen" else "claims"
        cfg_override = _override(cfg, point, key)

    q = QUESTIONS[point]
    res = router.decide(cfg_override, point, state, q, parse, run)
    return res.get("result"), res.get("confidence"), res.get("fallback_reason")


def screen_sources(run, ticket, search_out, attempt=1):
    """Screen search sources with deterministic heuristics and decision model."""
    tid = ticket.get("id", "") if isinstance(ticket, dict) else str(ticket)
    cfg = run.cfg
    sc = _cfg(cfg)
    max_items = sc.get("max_items", 25)
    escalate_on_unsure = sc.get("escalate_on_unsure", True)

    raw_sources = search_out.get("sources", []) if isinstance(search_out, dict) else []
    candidates, discarded = _dedupe_and_filter(raw_sources)

    kept = []
    unsure = []
    down = False
    model_calls = 0

    point = "source_screen"
    cfg_override = _override(cfg, point, "sources")
    title = ticket.get("title", "") if isinstance(ticket, dict) else ""
    desc = ticket.get("description", "") if isinstance(ticket, dict) else ""
    topic = "\n".join(x for x in (title, desc) if x)

    for src in candidates:
        url = src.get("url", "")
        if down or model_calls >= max_items:
            is_unsure = True
        else:
            state = _state_source(topic, src)
            model_calls += 1
            choice, conf, reason = _ask(run, cfg_override, point, state)
            if reason and reason.startswith("ollama failed"):
                down = True
                is_unsure = True
            elif choice == "keep":
                is_unsure = False
                kept.append(src)
            elif choice == "discard":
                is_unsure = False
                c_str = "%.2f" % conf if conf is not None else "0.00"
                discarded.append({
                    "url": url,
                    "reason": "decision model: discard (conf %s)" % c_str,
                    "by": "model",
                })
            else:
                is_unsure = True

        if is_unsure:
            kept.append(src)
            if escalate_on_unsure:
                unsure.append(url)

    if hasattr(run, "event"):
        run.event(
            "screen",
            ticket=tid,
            attempt=attempt,
            stage="search",
            kept=len(kept),
            discarded=len(discarded),
            unsure=len(unsure),
        )

    return {"kept": kept, "discarded": discarded, "unsure": unsure}


def screen_claims(run, ticket, research_out, kept_sources, attempt=1):
    """Screen research claims against kept search sources."""
    tid = ticket.get("id", "") if isinstance(ticket, dict) else str(ticket)
    cfg = run.cfg
    sc = _cfg(cfg)
    max_items = sc.get("max_items", 25)
    escalate_on_unsure = sc.get("escalate_on_unsure", True)

    kept_by_url = {}
    for s in (kept_sources or []):
        if isinstance(s, dict) and s.get("url"):
            kept_by_url[norm_url(s["url"])] = s

    supported = []
    unsupported = []
    escalated = []
    conf_by_id = {}
    down = False
    model_calls = 0

    point = "claim_screen"
    cfg_override = _override(cfg, point, "claims")
    claims = research_out.get("claims", []) if isinstance(research_out, dict) else []

    for c in claims:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not cid or not isinstance(cid, str):
            unsupported.append({"id": cid if isinstance(cid, str) else "", "reason": "missing claim id"})
            continue
        source_urls = c.get("source_urls") or []
        norms = [norm_url(u) for u in source_urls if isinstance(u, str)]
        cited_kept = [kept_by_url[n] for n in norms if n in kept_by_url]

        if not source_urls or not cited_kept:
            unsupported.append({"id": cid, "reason": "no cited source among kept sources"})
            continue

        if down or model_calls >= max_items:
            is_unsure = True
            conf = None
        else:
            excerpts = [(s.get("url", ""), s.get("excerpt", "")) for s in cited_kept]
            state = _state_claim(c, excerpts)
            model_calls += 1
            choice, conf, reason = _ask(run, cfg_override, point, state)
            if reason and reason.startswith("ollama failed"):
                down = True
                is_unsure = True
            elif choice == "supported":
                is_unsure = False
                supported.append(cid)
                conf_by_id[cid] = conf if conf is not None else 0.0
            elif choice == "unsupported":
                is_unsure = False
                c_str = "%.2f" % conf if conf is not None else "0.00"
                unsupported.append({
                    "id": cid,
                    "reason": "decision model: unsupported (conf %s)" % c_str,
                })
            else:
                is_unsure = True

        if is_unsure:
            if escalate_on_unsure:
                escalated.append(cid)
            else:
                supported.append(cid)
                conf_by_id[cid] = conf if conf is not None else 0.0

    if hasattr(run, "event"):
        run.event(
            "screen",
            ticket=tid,
            attempt=attempt,
            stage="research",
            supported=len(supported),
            unsupported=len(unsupported),
            escalated=len(escalated),
        )

    return {
        "supported": supported,
        "unsupported": unsupported,
        "escalated": escalated,
        "conf_by_id": conf_by_id,
    }


def synthetic_factcheck(research_out, kept_sources, screen_state):
    """Build factcheck-shaped dict for passed screen."""
    claims_list = research_out.get("claims", []) if isinstance(research_out, dict) else []
    claims_by_id = {c.get("id"): c for c in claims_list if isinstance(c, dict) and isinstance(c.get("id"), str) and c.get("id")}
    conf_by_id = screen_state.get("conf_by_id", {}) if isinstance(screen_state, dict) else {}

    claims = []
    for cid in (screen_state.get("supported", []) if isinstance(screen_state, dict) else []):
        if not isinstance(cid, str) or not cid:
            continue
        c = claims_by_id.get(cid, {})
        conf = conf_by_id.get(cid, 0.0)
        evidence = "screened by decision model (conf %.2f)" % (conf if conf is not None else 0.0)
        claims.append({
            "id": cid,
            "claim": c.get("claim", ""),
            "verdict": "supported",
            "evidence": evidence,
            "source_urls": list(c.get("source_urls") or []),
            "correction": None,
        })

    sources = []
    for s in (kept_sources or []):
        if isinstance(s, dict):
            sources.append({
                "url": s.get("url", ""),
                "status": "ok",
                "note": "screened by decision model",
            })

    return {
        "verdict": "pass",
        "feedback": "",
        "summary": "screened by decision model",
        "claims": claims,
        "sources": sources,
        "screened": True,
    }


def merge_supported(fc_out, research_out, screen_state):
    """Copy fc_out with synthetic supported entries appended for ids in screen_state['supported']
    not already in fc_out['claims'] (compare by id).
    """
    out = copy.deepcopy(fc_out) if isinstance(fc_out, dict) else {}
    if "claims" not in out or not isinstance(out["claims"], list):
        out["claims"] = []
    if "sources" not in out or not isinstance(out["sources"], list):
        out["sources"] = []
    existing_ids = {c.get("id") for c in out["claims"] if isinstance(c, dict) and isinstance(c.get("id"), str)}
    claims_list = research_out.get("claims", []) if isinstance(research_out, dict) else []
    claims_by_id = {c.get("id"): c for c in claims_list if isinstance(c, dict) and isinstance(c.get("id"), str) and c.get("id")}
    conf_by_id = screen_state.get("conf_by_id", {}) if isinstance(screen_state, dict) else {}

    for cid in (screen_state.get("supported", []) if isinstance(screen_state, dict) else []):
        if not isinstance(cid, str) or not cid:
            continue
        if cid not in existing_ids:
            c = claims_by_id.get(cid, {})
            conf = conf_by_id.get(cid, 0.0)
            evidence = "screened by decision model (conf %.2f)" % (conf if conf is not None else 0.0)
            out["claims"].append({
                "id": cid,
                "claim": c.get("claim", ""),
                "verdict": "supported",
                "evidence": evidence,
                "source_urls": list(c.get("source_urls") or []),
                "correction": None,
            })
            existing_ids.add(cid)
    return out


def should_skip(cfg, screen_state):
    """Check if factcheck should be skipped."""
    if not isinstance(cfg, dict):
        return False
    if not screen_state or not isinstance(screen_state, dict):
        return False
    sc = _cfg(cfg)
    if not sc.get("skip_factcheck"):
        return False
    if not screen_state.get("supported"):
        return False
    if screen_state.get("escalated"):
        return False
    if screen_state.get("unsure_sources"):
        return False
    return True


def write_audit(run, tid, attempt, screen_state):
    """Write screen state to tickets/<tid>/attempt-<n>/screen.json atomically. Never raises."""
    if not run or not hasattr(run, "write_json"):
        return
    try:
        run.write_json("tickets/%s/attempt-%d/screen.json" % (tid, int(attempt)), screen_state)
    except (OSError, TypeError, ValueError):
        pass
