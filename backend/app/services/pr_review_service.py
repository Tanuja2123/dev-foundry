"""
On-demand security review of a pull request from the chat (FEATURES.md SEC-A8): the Security_Review_Agent operations.

LIST_PULL_REQUESTS: code only. The repository's active pull requests.
REVIEW_PULL_REQUEST: reads the PR's changed files (ado_pull_requests), runs the rule-based checks (security_scan) and the
    AI review (Security_Review_Agent, typed findings validated against the diff), stores the review
    (Repository.pr_reviews) and shows it. Nothing is posted.
POST_PR_REVIEW_COMMENTS: code only, after explicit approval. Posts one inline comment per finding plus a summary;
    findings already posted (by fingerprint, in the database or on the PR) are never posted twice.
Replies are rendered by code (no synthesizer), so what is shown is exactly what was stored and will be posted.
"""
import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from agent_framework import Message

from app.core.logger import logger
from app.services import ado_pull_requests as ado
from app.services import progress
from app.services import project_store as store
from app.services import security_scan
from app.services.agents.security_review_agent import AiReview, get_security_review_agent
from app.services.llm_client import AllProvidersFailedError
from app.services.tech_fit import looks_affirmative

MIGRATION_007_MESSAGE = ("Pull request reviews need a one-time database update first: run "
                         "`backend/db/migrations/007_pr_reviews.sql` in the Supabase SQL editor, then ask again.")
NO_REPOSITORY_MESSAGE = ("Pull requests are reviewed in the project's Azure DevOps repository, and it hasn't been "
                         "created yet. Create the repository first; then open a pull request and ask me to review it.")
NO_REVIEW_MESSAGE = "There is no security review to post yet. Ask me to review a pull request first."
POST_QUESTION = "Shall I post the findings of PR #{pr_id} as comments on the pull request now?"
POST_MARKER = "as comments on the pull request"
COMMENT_ID_PREFIX = "DevFoundry security review · id "
SEVERITY_LABELS = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}
DEFAULT_CHUNK_CHARS = 10_000
DEFAULT_MAX_CHUNKS = 6
MIN_AI_CONFIDENCE = 0.6
_PR_NUMBER = re.compile(r"(?:\bPR|\bpull\s+request|\bpullrequest)\s*(?:#|no\.?|number)?\s*(\d{1,7})\b|#(\d{1,7})\b",
                        re.IGNORECASE)
_POST_REQUEST = re.compile(r"\b(post\w*|comment\w*|publish\w*)\b", re.IGNORECASE)
_OFFER_TO_POST = re.compile(r"\b(post|add|publish)\b[^?]{0,140}\b(findings?|comments?)\b[^?]{0,80}\?", re.IGNORECASE)
_SECRET_WORDS = re.compile(r"secret|password|credential|token|api[ -]?key|private key", re.IGNORECASE)


def review_chunk_chars() -> int:
    try:
        return max(2_000, int(os.getenv("PR_REVIEW_CHUNK_CHARS", DEFAULT_CHUNK_CHARS)))
    except ValueError:
        return DEFAULT_CHUNK_CHARS


def review_max_chunks() -> int:
    try:
        return max(1, int(os.getenv("PR_REVIEW_MAX_CHUNKS", DEFAULT_MAX_CHUNKS)))
    except ValueError:
        return DEFAULT_MAX_CHUNKS


def pr_number(user_query: str, decided: Optional[int] = None) -> Optional[int]:
    """The PR the user means: the Orchestrator's field, else "PR 12" / "pull request #12" / "#12" in the message."""
    if decided:
        return int(decided)
    match = _PR_NUMBER.search(user_query or "")
    return int(match.group(1) or match.group(2)) if match else None


def latest_review(repository: Dict[str, Any], pr_id: Optional[int] = None) -> Tuple[Optional[int], Optional[Dict]]:
    reviews = repository.get("pr_reviews") or {}
    if pr_id is not None:
        return pr_id, reviews.get(str(pr_id))
    if not reviews:
        return None, None
    key = max(reviews, key=lambda k: reviews[k].get("reviewed_at") or "")
    return int(key), reviews[key]


_PR_MENTION = re.compile(r"\bPRs?\b|\bpull\s+requests?\b|\bpullrequests?\b|\bfindings?\b", re.IGNORECASE)
_REVIEW_VERB = re.compile(r"\b(review\w*|check\w*|scan\w*|audit\w*|analy[sz]\w*)\b", re.IGNORECASE)
_QUESTION_START = re.compile(r"^\s*(why|what|how|when|where|who|did|does|do|was|were|is|are|has|have|should)\b",
                             re.IGNORECASE)


def explicit_operation(user_query: str, allowed: List[str]) -> Optional[str]:
    """
    A message that clearly asks to post review findings or to review a PR always runs that operation, so code checks
    the facts (new commits, what was posted) instead of the Orchestrator answering from memory (seen live: "post the
    findings" after a new commit was answered "already posted, the PR is up to date"). Questions are left to the model.
    """
    text = user_query or ""
    if not _PR_MENTION.search(text) or _QUESTION_START.match(text):
        return None
    if _POST_REQUEST.search(text) and "POST_PR_REVIEW_COMMENTS" in allowed:
        return "POST_PR_REVIEW_COMMENTS"
    if _REVIEW_VERB.search(text) and _PR_NUMBER.search(text) and "REVIEW_PULL_REQUEST" in allowed:
        return "REVIEW_PULL_REQUEST"
    return None


def is_post_request(user_query: str, history: List[Dict[str, Any]]) -> bool:
    """Posting needs a message that asks to post/comment, or a "yes" to the assistant's posting question."""
    if _POST_REQUEST.search(user_query or ""):
        return True
    last = next((m.get("text") or "" for m in reversed(history or []) if m.get("role") == "assistant"), "")
    # The offer to post may be worded by the model ("Would you like me to post the findings as comments on PR #5?"),
    # not only by our code (seen: "yes" was asked again, ISSUES AGT-35)
    offered = POST_MARKER in last or bool(_OFFER_TO_POST.search(last))
    return offered and looks_affirmative(user_query)


async def run_pr_operation(client: Any, operation: str, user_query: str, decided_pr: Optional[int],
                           state: Dict[str, Any]) -> str:
    project, repository = state.get("project") or {}, state.get("repository") or {}
    if not repository.get("ado_repo_id") or not project.get("ado_project_id"):
        return NO_REPOSITORY_MESSAGE
    if not store.has_pr_review_column(repository):
        return MIGRATION_007_MESSAGE
    project_ref, repo_id = project["ado_project_id"], repository["ado_repo_id"]
    try:
        if operation == "LIST_PULL_REQUESTS":
            progress.step("Reading the pull requests")
            return render_pull_request_list(await asyncio.to_thread(ado.list_pull_requests, project_ref, repo_id),
                                            repository.get("repo_name"))
        pr_id = pr_number(user_query, decided_pr)
        if operation == "POST_PR_REVIEW_COMMENTS":
            pr_id, review = latest_review(repository, pr_id)
            if not review:
                return NO_REVIEW_MESSAGE if pr_id is None else (
                    f"PR #{pr_id} hasn't been reviewed yet. Ask me to review it first; nothing was posted.")
            return await post_review(project_ref, repository, pr_id, review)
        if pr_id is None:
            progress.step("Reading the pull requests")
            active = await asyncio.to_thread(ado.list_pull_requests, project_ref, repo_id)
            if len(active) != 1:
                return render_pull_request_list(active, repository.get("repo_name"), asked_to_review=True)
            pr_id = active[0]["id"]
        return await review_pull_request(client, project_ref, repository, pr_id)
    except ado.PullRequestError as exc:
        logger.error(f"[PR Review] {exc.error_type}: {exc.message} {exc.details}")
        return f"{exc.message} Nothing was changed.\n\n---\n\nCheck the pull request number and try again."


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

async def review_pull_request(client: Any, project_ref: str, repository: Dict[str, Any], pr_id: int) -> str:
    progress.step(f"Reading pull request #{pr_id}")
    diff = await asyncio.to_thread(ado.get_pull_request_diff, project_ref, repository["ado_repo_id"], pr_id)
    progress.step("Checking the changes for secrets and risky settings")
    progress.detail(f"{len(diff.scanned)} of {len(diff.files)} changed files")
    rule_findings = security_scan.scan_files(diff.files)
    progress.step("Reviewing the changes with AI")
    ai_findings, ai_note = await ai_review(client, diff, rule_findings)
    findings = security_scan.dedupe(rule_findings + ai_findings)

    previous = (repository.get("pr_reviews") or {}).get(str(pr_id)) or {}
    pr = diff.pr
    review = {
        "pr_id": pr_id, "title": pr.get("title"), "url": pr.get("url"), "source": pr.get("source"),
        "target": pr.get("target"), "author": pr.get("author"), "status": pr.get("status"),
        "iteration": pr.get("iteration"), "source_commit": pr.get("source_commit"),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "files_changed": len(diff.files), "files_scanned": len(diff.scanned),
        "skipped": skipped_summary(diff), "ai_note": ai_note,
        "findings": findings, "counts": security_scan.counts(findings),
        "posted": previous.get("posted") or {},        # posted earlier (by fingerprint): never posted twice
    }
    store.save_pr_review(repository, pr_id, review)
    logger.info(f"[PR Review] PR #{pr_id} iteration {review['iteration']}: {len(rule_findings)} rule findings, "
                f"{len(ai_findings)} AI findings -> {len(findings)} stored")
    return render_review(review)


def skipped_summary(diff: ado.PullRequestDiff) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for changed in diff.skipped:
        summary[changed.skipped] = summary.get(changed.skipped, 0) + 1
    return summary


def review_chunks(diff: ado.PullRequestDiff, chunk_chars: Optional[int] = None) -> List[str]:
    """The added lines (with context and line numbers) split into parts that fit one AI request."""
    limit = chunk_chars or review_chunk_chars()
    chunks: List[str] = []
    current = ""
    for changed in diff.scanned:
        if not changed.added:
            continue
        block = [f"=== FILE: {changed.path} ({changed.change_type}) ==="]
        for start, end in ado.hunks(changed):
            if len(block) > 1:
                block.append("...")
            for number in range(start, end + 1):
                marker = "+" if number in changed.added else " "
                block.append(f"{number} {marker} {changed.lines[number - 1][:400]}")
        text = "\n".join(block) + "\n"
        while len(text) > limit:                      # one huge file: split it on line boundaries
            cut = text.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(text[:cut])
            text = f"=== FILE: {changed.path} ({changed.change_type}, continued) ===\n" + text[cut:].lstrip("\n")
        if current and len(current) + len(text) > limit:
            chunks.append(current)
            current = ""
        current += text
    if current:
        chunks.append(current)
    return chunks


def _mask_quoted(text: str) -> str:
    return re.sub(r"([\"'`])([^\"'`\s]{8,})\1", lambda m: m.group(1) + security_scan._mask(m.group(2)) + m.group(1),
                  text)


def validate_ai_findings(raw: List[Any], diff: ado.PullRequestDiff,
                         rule_findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keeps AI findings that point at a line the PR adds (snapped by up to 2 lines), not already found by a rule."""
    files = {f.path: f for f in diff.scanned}
    taken = {(f["file"], f.get("line")) for f in rule_findings}
    result = []
    for item in raw:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        changed = files.get(str(data.get("file") or "").strip()) or files.get("/" + str(data.get("file") or "").lstrip("/"))
        try:
            line, confidence = int(data.get("line") or 0), float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if not changed or confidence < MIN_AI_CONFIDENCE:
            continue
        if line not in changed.added:
            near = [n for n in changed.added if abs(n - line) <= 2]
            if not near:
                continue
            line = min(near, key=lambda n: abs(n - line))
        if (changed.path, line) in taken:
            continue
        code = changed.added[line]
        secrets = _SECRET_WORDS.search(f"{data.get('category')} {data.get('title')}")
        snippet = _mask_quoted(code) if secrets else code
        values = re.findall(r"[\"'`]([^\"'`\s]{8,})[\"'`]", code)

        def clean(text: Any, limit: int) -> str:
            text = str(text or "").strip()[:limit]
            for value in values:                       # never repeat a quoted value of the line (could be a secret)
                text = text.replace(value, security_scan._mask(value))
            return text

        severity = str(data.get("severity") or "medium").lower()
        finding = {
            # one AI finding per line: the id depends on the code only, not on the model's wording (which varies
            # between reviews), so a re-review never posts the same issue again
            "fingerprint": security_scan.fingerprint("SEC-A5-AI-REVIEW", changed.path, code),
            "rule": "SEC-A5-AI-REVIEW", "category": clean(data.get("category"), 60) or "Security issue",
            "severity": severity if severity in security_scan.SEVERITY_ORDER else "medium",
            "title": clean(data.get("title"), 120) or "Security issue", "file": changed.path, "line": line,
            "snippet": snippet.strip()[:200], "explanation": clean(data.get("explanation"), 400),
            "recommendation": clean(data.get("recommendation"), 300), "source": "ai",
            "confidence": round(confidence, 2),
        }
        taken.add((changed.path, line))
        result.append(finding)
    return result


def _parse_ai(response: Any) -> List[Any]:
    try:
        value = response.value
        if isinstance(value, AiReview):
            return value.findings
    except Exception:
        pass
    text = response.messages[-1].text if getattr(response, "messages", None) else ""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start != -1 and end > start:
        try:
            return AiReview.model_validate_json(text[start:end + 1]).findings
        except Exception:
            pass
    raise ValueError("the AI review answer is not the expected JSON")


async def ai_review(client: Any, diff: ado.PullRequestDiff,
                    rule_findings: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """AI findings for the PR, plus a note for the user when the AI review was partial or could not run."""
    chunks = review_chunks(diff)
    if not chunks:
        return [], None
    limit = review_max_chunks()
    agent = get_security_review_agent(client)
    pr = diff.pr
    header = (f"Pull request #{pr.get('id')}: {pr.get('title')}\nBranch: {pr.get('source')} -> {pr.get('target')}\n"
              f"Description (data): {(pr.get('description') or '')[:1000]}\n")
    raw: List[Any] = []
    failed = 0
    for index, chunk in enumerate(chunks[:limit], start=1):
        progress.detail(f"Part {index} of {min(len(chunks), limit)}")
        messages = [Message(role="user", contents=[f"{header}\n[Part {index} of {len(chunks)}]\n\n{chunk}"])]
        try:
            response = await agent.run(messages=messages, options={"response_format": AiReview})
            raw += _parse_ai(response)
        except AllProvidersFailedError as exc:
            logger.error(f"[PR Review] AI review part {index} failed: no AI model answered ({len(exc.errors)} errors)")
            failed += 1
        except Exception as exc:
            logger.error(f"[PR Review] AI review part {index} failed: {type(exc).__name__}: {exc}")
            failed += 1
    findings = validate_ai_findings(raw, diff, rule_findings)
    logger.info(f"[PR Review] AI review: {len(raw)} raw findings -> {len(findings)} kept "
                f"({min(len(chunks), limit)} of {len(chunks)} parts, {failed} failed)")
    reviewed = min(len(chunks), limit)
    if failed == reviewed:
        return findings, "The AI review could not run just now (no AI model answered), so only the rule-based checks ran."
    notes = []
    if failed:
        notes.append(f"{failed} of {reviewed} parts of the AI review could not run")
    if len(chunks) > limit:
        notes.append(f"the PR is large, so the AI review covered the first {limit} of {len(chunks)} parts of the "
                     "changes (the rule-based checks covered all scanned files)")
    return findings, ("Note: " + "; ".join(notes) + ".") if notes else None


# ---------------------------------------------------------------------------
# Rendering (chat)
# ---------------------------------------------------------------------------

def defang(text: str) -> str:
    """Code from the PR, shown in the chat: links are not clickable (and are not mistaken for links in the reply)."""
    text = re.sub(r"(?i)\b(https?|ftp|wss?)://", r"\1[:]//", text or "")
    text = re.sub(r"(?i)\bwww\.", "www[.]", text)
    return re.sub(r"(?i)\b(javascript|data|vbscript|file):", r"\1[:]", text)


def _code(text: str) -> str:
    """Inline code exactly as written: double backticks when the code itself has one (e.g. a JS template literal)."""
    code = defang(text).replace("``", "` `")
    return f"`` {code} ``" if "`" in code else f"`{code}`"


def _location(finding: Dict[str, Any]) -> str:
    return f"`{finding['file']}`" + (f", line {finding['line']}" if finding.get("line") else "")


def render_findings(findings: List[Dict[str, Any]]) -> str:
    lines = []
    for severity in security_scan.SEVERITY_ORDER:
        group = [f for f in findings if f.get("severity") == severity]
        if not group:
            continue
        lines += [f"### {SEVERITY_LABELS[severity]} ({len(group)})", ""]
        for number, finding in enumerate(group, start=1):
            found_by = "AI review" if finding.get("source") == "ai" else "rule " + finding.get("rule", "")
            lines.append(f"{number}. **{defang(finding['title'])}**: {_location(finding)} ({found_by})")
            if finding.get("snippet") and finding.get("line"):
                lines.append(f"   - Code: {_code(finding['snippet'])}")
            lines.append(f"   - Why: {defang(finding.get('explanation') or '')}")
            lines.append(f"   - Fix: {defang(finding.get('recommendation') or '')}")
        lines.append("")
    return "\n".join(lines).strip()


def _count_line(counts: Dict[str, int]) -> str:
    return ", ".join(f"{counts.get(s, 0)} {s}" for s in security_scan.SEVERITY_ORDER if counts.get(s))


def render_review(review: Dict[str, Any]) -> str:
    pr_id, findings = review["pr_id"], review.get("findings") or []
    head = (f"Here is the security review of **[PR #{pr_id}]({review.get('url')}): {review.get('title')}** "
            f"(`{review.get('source')}` → `{review.get('target')}`, by {review.get('author') or 'unknown'}).")
    if review.get("status") and review["status"] != "active":
        head += f" Note: this pull request is **{review['status']}**."
    scope = f"Checked **{review.get('files_scanned')} of {review.get('files_changed')} changed files**"
    if review.get("skipped"):
        scope += " (skipped: " + ", ".join(f"{n} {reason}" for reason, n in review["skipped"].items()) + ")"
    scope += " with the rule-based checks and the AI review."
    parts = [head, scope]
    if review.get("ai_note"):
        parts.append(review["ai_note"])
    waiting = store.unposted_findings(review)
    if not findings:
        parts.append("**No security issues found** in the lines this pull request adds.")
        parts += ["---", f"There is nothing to post. Ask me to review PR #{pr_id} again after new commits."]
        return "\n\n".join(parts)
    parts.append(f"Found **{len(findings)} issue{'s' if len(findings) != 1 else ''}**: {_count_line(review['counts'])}.")
    parts.append(render_findings(findings))
    if len(waiting) < len(findings):
        parts.append(f"{len(findings) - len(waiting)} of these were already posted to the pull request earlier.")
    parts.append("---")
    # One closing sentence (two sentences after the divider were split again into a second divider, ISSUES AGT-31)
    if waiting:
        what = "the findings" if len(waiting) == len(findings) else \
            f"the {len(waiting)} new finding{'s' if len(waiting) != 1 else ''}"
        parts.append(f"Nothing is posted until you say so: say **post the findings to PR #{pr_id}** to add {what} as "
                     "comments on the pull request.")
    else:
        parts.append(f"Every finding is already on the pull request, so ask me to review PR #{pr_id} again after new "
                     "commits.")
    return "\n\n".join(parts)


def render_pull_request_list(prs: List[Dict[str, Any]], repo_name: Optional[str], asked_to_review: bool = False) -> str:
    repo = f"**{repo_name}**" if repo_name else "the repository"
    if not prs:
        return (f"There are no active pull requests in {repo}.\n\n---\n\nOpen a pull request in Azure DevOps, then ask "
                "me to review it (for example \"Review PR #1 for security issues\").")
    lines = [f"Active pull requests in {repo}:", ""]
    for pr in prs[:20]:
        draft = " (draft)" if pr.get("is_draft") else ""
        lines.append(f"- **[PR #{pr['id']}]({pr['url']})**: {pr['title']}{draft} (`{pr['source']}` → `{pr['target']}`, "
                     f"by {pr.get('author') or 'unknown'})")
    lines += ["", "---", ""]
    lines.append("Which one should I review? Say for example **review PR #" + str(prs[0]["id"]) + "**."
                 if asked_to_review else "Ask me to review one of them, for example **review PR #" + str(prs[0]["id"])
                 + " for security issues**.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Posting (after approval)
# ---------------------------------------------------------------------------

def comment_text(finding: Dict[str, Any]) -> str:
    found_by = "AI review" if finding.get("source") == "ai" else f"rule {finding.get('rule')}"
    lines = [f"**🔒 {SEVERITY_LABELS.get(finding.get('severity'), 'Medium')}: {finding['title']}**", "",
             finding.get("explanation") or "", "", f"**Fix:** {finding.get('recommendation') or ''}"]
    if finding.get("snippet") and finding.get("line"):
        lines += ["", "`" + finding["snippet"].replace("`", "'") + "`"]
    lines += ["", f"_{COMMENT_ID_PREFIX}{finding['fingerprint']} · {found_by}_"]
    return "\n".join(lines)


def summary_text(review: Dict[str, Any]) -> str:
    findings = review.get("findings") or []
    lines = [f"**🔒 DevFoundry security review** (iteration {review.get('iteration')})", "",
             f"{len(findings)} finding{'s' if len(findings) != 1 else ''}: {_count_line(review.get('counts') or {})}. "
             "Each finding is a comment on its line.", "",
             f"Checked {review.get('files_scanned')} of {review.get('files_changed')} changed files with rule-based "
             "checks (secrets, environment files, hard-coded configuration) and an AI review."]
    if review.get("ai_note"):
        lines += ["", review["ai_note"]]
    lines += ["", f"_{COMMENT_ID_PREFIX}summary-{review.get('iteration')}_"]
    return "\n".join(lines)


def posted_ids_on_pr(threads: List[Dict[str, Any]]) -> set:
    ids = set()
    for thread in threads:
        for comment in thread.get("comments") or []:
            for match in re.finditer(re.escape(COMMENT_ID_PREFIX) + r"([A-Za-z0-9-]+)", comment.get("content") or ""):
                ids.add(match.group(1))
    return ids


async def post_review(project_ref: str, repository: Dict[str, Any], pr_id: int, review: Dict[str, Any]) -> str:
    repo_id = repository["ado_repo_id"]
    progress.step(f"Posting the findings to PR #{pr_id}")
    current = await asyncio.to_thread(ado.get_pull_request, project_ref, repo_id, pr_id)
    if current.get("iteration") != review.get("iteration"):
        return (f"PR #{pr_id} has new commits since my review (iteration {review.get('iteration')} → "
                f"{current.get('iteration')}), so the line numbers may no longer match. Nothing was posted.\n\n---\n\n"
                f"Ask me to review PR #{pr_id} again, then post the findings.")
    on_pr = posted_ids_on_pr(await asyncio.to_thread(ado.list_threads, project_ref, repo_id, pr_id))
    posted = dict(review.get("posted") or {})
    findings = review.get("findings") or []
    threads = []
    for finding in findings:
        key = finding["fingerprint"]
        if key in posted or key in on_pr:
            continue
        threads.append({"key": key, "content": comment_text(finding), "file": finding["file"],
                        "line": finding.get("line"), "line_length": len(finding.get("snippet") or "") or 1})
    summary_key = f"summary-{review.get('iteration')}"
    if findings and threads and summary_key not in posted and summary_key not in on_pr:
        threads.append({"key": summary_key, "content": summary_text(review)})
    already = len([f for f in findings if f["fingerprint"] in posted or f["fingerprint"] in on_pr])
    if not threads:
        if not findings:
            return (f"The review of PR #{pr_id} found no issues, so there is nothing to post.\n\n---\n\n"
                    f"Ask me to review PR #{pr_id} again after new commits.")
        return (f"Every finding of PR #{pr_id} is already on the pull request, so nothing was posted again (no "
                f"duplicates). [Open PR #{pr_id}]({review.get('url')})\n\n---\n\n"
                f"Ask me to review PR #{pr_id} again after new commits; only new findings will be posted.")
    progress.begin_side_effect(f"Posting the review comments to PR #{pr_id}")   # Stop can no longer cancel the turn
    result = await asyncio.to_thread(ado.post_threads, project_ref, repo_id, pr_id, threads)
    posted.update(result.get("posted") or {})
    for key in on_pr:                                   # found on the PR (e.g. posted before a database reset)
        posted.setdefault(key, None)
    review = dict(review, posted=posted)
    store.save_pr_review(repository, pr_id, review)
    new_findings = len([k for k in result.get("posted") or {} if not k.startswith("summary-")])
    if result.get("error"):
        return (f"Some comments could not be posted to PR #{pr_id} ({new_findings} posted so far): "
                f"{result['error'].get('message')} Ask me to post the findings again to continue; the ones already "
                "posted will not be duplicated.")
    text = (f"Posted **{new_findings} finding{'s' if new_findings != 1 else ''}** to "
            f"[PR #{pr_id}]({review.get('url')}) as comments on their lines"
            + (", plus a summary comment" if summary_key in (result.get("posted") or {}) else "") + ".")
    if already:
        text += f" {already} finding{'s were' if already != 1 else ' was'} already on the pull request and not posted again."
    return text + (f"\n\n---\n\nAfter new commits, ask me to review PR #{pr_id} again; only new findings will be "
                   "posted.")
