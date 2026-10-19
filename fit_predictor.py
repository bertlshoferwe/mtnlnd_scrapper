"""
"Likely fit / likely not a fit" suggestions for projects still in To Review,
learned from the user's own past decisions (project_decisions — see
supabase_store.set_project_decision / build_project_snapshot).

How it works, per undecided matched project:
  1. Snapshot it the same way a decision is snapshotted (keywords and pages
     per matched file, AI notes, bid timing, plan holders).
  2. Find the most similar past decisions — embeddings when a Gemini key is
     set (cached per decision in project_decisions.embedding), otherwise
     keyword/site overlap — keeping both kinds (Jira and Not a fit) in the
     mix so the comparison isn't one-sided.
  3. Ask the AI to judge the new project against those examples, including
     the user's reasons and notes, and answer with a verdict + one plain
     sentence. With no AI configured, the similar examples just vote.

Stays off until the division has enough real decisions (the "start after"
setting in the dashboard's hidden settings window, default 50 — see
supabase_store.DEFAULT_SETTINGS) (legacy
"Unknown (from before)" ones and anything later moved back to review don't
count). Runs at the end of every scan (scraper._scan_division), or by hand:

    python fit_predictor.py <division_id>
"""

import json
import math
import os
import re
import sys
from datetime import datetime, timezone

import supabase_store

FIT_EXAMPLES = int(os.environ.get("FIT_EXAMPLES") or "8")
# Bounds AI cost/time on a big backlog; the rest get picked up next scan.
FIT_MAX_PER_RUN = int(os.environ.get("FIT_MAX_PER_RUN") or "80")
# Re-judge an already-suggested project once the pool of past decisions has
# grown by this fraction (or at least FIT_REFRESH_MIN_NEW decisions).
FIT_REFRESH_GROWTH = 0.15
FIT_REFRESH_MIN_NEW = 5

VERDICTS = ("fit", "not_fit")


# ---------------------------------------------------------------------------
# Learning examples
# ---------------------------------------------------------------------------

def load_examples(division_id):
    """The decisions to learn from: the latest user decision per project,
    only where that's still Jira or Not a fit (a project later moved back to
    review is an open question again, not a label). Each item:
    {"id", "project_key", "decision", "reason", "note", "snapshot",
    "embedding", "prediction"}."""
    rows = supabase_store.load_decision_log(division_id)
    latest = {}
    for r in rows:  # newest first
        if r["project_key"] not in latest:
            latest[r["project_key"]] = r
    return [
        r for r in latest.values()
        if r.get("source") == "user" and r["decision"] in ("jira", "not_fit")
        and r.get("reason") != supabase_store.LEGACY_REASON and r.get("snapshot")
    ]


def snapshot_text(snap):
    """A compact, embedding- and prompt-friendly description of a project
    snapshot."""
    if not snap:
        return ""
    lines = [f"Site: {snap.get('site') or '?'}"]
    if snap.get("days_to_bid") is not None:
        lines.append(f"Days until bid: {snap['days_to_bid']}")
    lines.append(f"Files: {snap.get('file_count', 0)} total, "
                 f"{snap.get('matched_file_count', 0)} matched keywords")
    kws = snap.get("keywords") or {}
    if kws:
        top = sorted(kws.items(), key=lambda kv: -kv[1])[:20]
        lines.append("Keywords (files matched): " + ", ".join(f"{k} ({n})" for k, n in top))
    folders = snap.get("unmatched_by_folder") or {}
    if folders:
        lines.append("Unmatched files by folder: "
                     + ", ".join(f"{k} {v}" for k, v in sorted(folders.items(), key=lambda kv: -kv[1])[:8]))
    matched = snap.get("matched_files") or []
    for m in matched[:6]:
        name = (m.get("file") or "").split(" - ", 1)[-1].split("/")[-1]
        line = f"- {name}: {', '.join(m.get('keywords') or [])}"
        if m.get("ai_notes"):
            line += f" — {m['ai_notes'][:200]}"
        lines.append(line)
    if len(matched) > 6:
        lines.append(f"- …and {len(matched) - 6} more matched files")
    ph = snap.get("plan_holders")
    if ph:
        roles = ", ".join(f"{k} {v}" for k, v in sorted((ph.get("roles") or {}).items(), key=lambda kv: -kv[1]))
        lines.append(f"Plan holders: {ph.get('count', 0)} ({roles})")
    return "\n".join(lines)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _overlap(a, b):
    """No-embedding fallback similarity: keyword overlap (weighted by how many
    files hit each), plus a little for same site / same folder mix."""
    ka, kb = a.get("keywords") or {}, b.get("keywords") or {}
    keys = set(ka) | set(kb)
    kw = (sum(min(ka.get(k, 0), kb.get(k, 0)) for k in keys)
          / sum(max(ka.get(k, 0), kb.get(k, 0)) for k in keys)) if keys else 0.0
    site = 1.0 if a.get("site") and a.get("site") == b.get("site") else 0.0
    fa, fb = set(a.get("unmatched_by_folder") or {}), set(b.get("unmatched_by_folder") or {})
    folder = len(fa & fb) / len(fa | fb) if (fa | fb) else 0.0
    return 0.7 * kw + 0.2 * site + 0.1 * folder


def pick_similar(target_snap, target_vec, examples, k=FIT_EXAMPLES):
    """Top-k most similar examples, keeping at least a couple of each
    decision in the set when both exist."""
    scored = []
    for ex in examples:
        if target_vec is not None and ex.get("embedding"):
            sim = _cosine(target_vec, ex["embedding"])
        else:
            sim = _overlap(target_snap, ex["snapshot"])
        scored.append((sim, ex))
    scored.sort(key=lambda t: -t[0])
    chosen = scored[:k]
    for want in ("jira", "not_fit"):
        have = sum(1 for _, ex in chosen if ex["decision"] == want)
        if have >= 2:
            continue
        extra = [t for t in scored[k:] if t[1]["decision"] == want][:2 - have]
        # Make room by dropping the least similar examples of the other kind.
        for _ in extra:
            for j in range(len(chosen) - 1, -1, -1):
                if chosen[j][1]["decision"] != want:
                    chosen.pop(j)
                    break
        chosen += extra
    chosen.sort(key=lambda t: -t[0])
    return chosen


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------

PROMPT = """You help an estimator at Mountainland Supply, a construction materials supplier, decide which public bid projects are worth pursuing. Many projects match their product keywords but turn out to be mostly paperwork, too small, out of area, or not for products they sell.

Below are past projects the estimator already decided on, most similar first. "ADDED TO JIRA" means worth pursuing. "NOT A FIT" includes their reason, and sometimes a note in their own words. Learn their judgment from these — especially the reasons and notes — rather than from generic assumptions.

{examples}

NEW PROJECT TO JUDGE:
{target}

Reply with ONLY a JSON object, no other text:
{{"verdict": "fit" or "not_fit", "reason": one of {reasons} (only when verdict is not_fit, else null), "confidence": a number 0-100, "explanation": one short plain-English sentence for the estimator, naming the past project(s) it most resembles if that helps}}"""


def _example_block(i, ex, sim):
    head = (f"PAST PROJECT {i} — ADDED TO JIRA" if ex["decision"] == "jira"
            else f"PAST PROJECT {i} — NOT A FIT ({ex.get('reason') or 'no reason'})")
    if ex.get("note"):
        head += f'\nEstimator\'s note: "{ex["note"]}"'
    name = (ex["snapshot"] or {}).get("project") or ex["project_key"]
    return f"{head}\nProject: {name}\n{snapshot_text(ex['snapshot'])}"


def judge_with_ai(ai_client, target_snap, similar):
    examples = "\n\n".join(_example_block(i + 1, ex, sim) for i, (sim, ex) in enumerate(similar))
    target = f"Project: {target_snap.get('project')}\n{snapshot_text(target_snap)}"
    prompt = PROMPT.format(examples=examples, target=target,
                           reasons=json.dumps(list(supabase_store.NOT_FIT_REASONS)))
    raw = ai_client.complete(prompt, 400)
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return None
    verdict = out.get("verdict")
    if verdict not in VERDICTS:
        return None
    reason = out.get("reason") if verdict == "not_fit" else None
    if reason not in supabase_store.NOT_FIT_REASONS:
        reason = None
    try:
        conf = max(0, min(100, int(float(out.get("confidence") or 0))))
    except (TypeError, ValueError):
        conf = 50
    return {"verdict": verdict, "reason": reason, "confidence": conf,
            "explanation": (out.get("explanation") or "").strip()[:400] or None}


def judge_by_vote(similar):
    """No AI configured: similarity-weighted vote among the closest examples."""
    weight = {"jira": 0.0, "not_fit": 0.0}
    reasons = {}
    for sim, ex in similar:
        w = max(sim, 0.01)
        weight[ex["decision"]] += w
        if ex["decision"] == "not_fit" and ex.get("reason"):
            reasons[ex["reason"]] = reasons.get(ex["reason"], 0) + w
    total = weight["jira"] + weight["not_fit"]
    if not total:
        return None
    verdict = "fit" if weight["jira"] >= weight["not_fit"] else "not_fit"
    n = sum(1 for _, ex in similar if ex["decision"] == ("jira" if verdict == "fit" else "not_fit"))
    # Confidence = how many of the similar projects agree, not the weighted
    # share — the two kept-for-balance examples are often near-zero
    # similarity, which would otherwise read as a near-certain 100%.
    share = n / len(similar)
    reason = max(reasons, key=reasons.get) if verdict == "not_fit" and reasons else None
    expl = (f"{n} of the {len(similar)} most similar past projects went to Jira."
            if verdict == "fit" else
            f"{n} of the {len(similar)} most similar past projects were marked not a fit"
            + (f", mostly \"{reason}\"." if reason else "."))
    return {"verdict": verdict, "reason": reason, "confidence": int(round(share * 100)),
            "explanation": expl}


# ---------------------------------------------------------------------------
# Division run
# ---------------------------------------------------------------------------

def _needs_refresh(project, current, n_examples):
    if not current or not current.get("predicted_at"):
        return True
    if (project.get("latest_date") or "") > current["predicted_at"]:
        return True  # new documents since the last suggestion
    basis = current.get("basis_count") or 0
    grown = n_examples - basis
    return grown >= max(FIT_REFRESH_MIN_NEW, int(basis * FIT_REFRESH_GROWTH))


def predict_division(division_id, ai_client=None, embedder=None, progress=None):
    """Suggest fit / not a fit for every matched project in To Review.
    Returns the number of projects (re)judged. Never raises for a single
    project's failure."""
    settings = supabase_store.get_division_settings(division_id)
    if not settings["fit_enabled"]:
        print("  Fit suggestions: turned off for this division")
        return 0
    min_decisions = settings["fit_min_decisions"]
    examples = load_examples(division_id)
    if len(examples) < min_decisions:
        print(f"  Fit suggestions: {len(examples)} of {min_decisions} decisions so far — "
              f"not enough to suggest yet")
        return 0

    candidates = supabase_store.list_review_projects(division_id)
    current = supabase_store.load_project_fit(division_id)
    todo = [p for p in candidates if _needs_refresh(p, current.get(p["key"]), len(examples))]
    todo = todo[:FIT_MAX_PER_RUN]
    print(f"  Fit suggestions: {len(examples)} past decisions, "
          f"{len(todo)} of {len(candidates)} project(s) to (re)judge")
    if not todo:
        return 0

    # Embeddings: fill in any past decision missing one, then embed the
    # candidates in one batch. Any failure falls back to keyword overlap.
    target_vecs = {}
    if embedder is not None:
        missing = [ex for ex in examples if not ex.get("embedding")]
        if missing:
            vecs = embedder.embed([snapshot_text(ex["snapshot"]) for ex in missing])
            if vecs:
                for ex, v in zip(missing, vecs):
                    ex["embedding"] = v
                supabase_store.save_decision_embeddings({ex["id"]: ex["embedding"] for ex in missing})

    snaps = {}
    for p in todo:
        try:
            snaps[p["key"]] = supabase_store.build_project_snapshot(division_id, p["key"])
        except Exception as e:
            print(f"  ! couldn't snapshot {p['key']}: {e}")
    if embedder is not None and snaps:
        keys = list(snaps)
        vecs = embedder.embed([snapshot_text(snaps[k]) for k in keys])
        if vecs:
            target_vecs = dict(zip(keys, vecs))

    done = 0
    now = datetime.now(timezone.utc).isoformat()
    for i, (key, snap) in enumerate(snaps.items()):
        if progress:
            progress(i, len(snaps))
        similar = pick_similar(snap, target_vecs.get(key), examples)
        if not similar:
            continue
        result = judge_with_ai(ai_client, snap, similar) if ai_client else None
        if result is None:
            result = judge_by_vote(similar)
        if result is None:
            continue
        result.update({"predicted_at": now, "basis_count": len(examples),
                       "similar": [ex["project_key"] for _, ex in similar[:3]]})
        supabase_store.save_project_fit(division_id, key, result)
        done += 1
    print(f"  Fit suggestions: {done} project(s) judged")
    return done


if __name__ == "__main__":
    from dotenv import load_dotenv
    import ai_provider
    load_dotenv()
    if len(sys.argv) < 2:
        print("usage: python fit_predictor.py <division_id>")
        sys.exit(1)
    predict_division(sys.argv[1], ai_provider.get_provider(), ai_provider.get_embedder())
