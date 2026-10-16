"""
Rule-based security checks on the lines a pull request ADDS (FEATURES.md SEC-A1 / SEC-A2 / SEC-A3). No LLM involved.

- SEC-A1 secrets: known key formats (cloud, Git hosting, payment, chat tokens, private keys, JWTs), credentials inside
  connection strings, and secret-looking assignments (`password = "..."`) whose value is not a placeholder.
- SEC-A2 environment leakage: committed .env / key files, secrets used as the default of an environment variable.
- SEC-A3 hard-coded configuration: non-local http(s) URLs and IP addresses in source code (belong in config).

Every finding: {"fingerprint", "rule", "category", "severity", "title", "file", "line", "snippet", "explanation",
"recommendation", "source": "rule", "confidence"}. Secret values are always masked: neither the chat nor a PR comment
ever repeats a secret.
"""
import hashlib
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_PLACEHOLDER = re.compile(
    r"(change[-_]?me|your[-_ ]|xxx|\*\*\*|<[^>]+>|\$\{|\{\{|example|sample|dummy|placeholder|redacted|replace[-_]?me|"
    r"todo|fake|test[-_]?(key|secret|token|password)|^(password|secret|token|none|null|undefined)$)",
    re.IGNORECASE)
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|fixtures?|mocks?)(/|$)|[._-](test|spec)\.[a-z]+$",
                        re.IGNORECASE)
_DOC_EXTENSIONS = (".md", ".mdx", ".rst", ".txt", ".adoc")
_CONFIG_PATH = re.compile(
    r"(^|/)(config|configs|settings|constants?|env)(/|\.|$)|\.(env\.example|env\.sample|env\.template|ya?ml|toml|ini|"
    r"cfg|conf|properties|json)$|(^|/)(dockerfile|docker-compose[^/]*|\.github/.*|azure-pipelines[^/]*)$",
    re.IGNORECASE)
_SOURCE_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".cs", ".go", ".rb", ".php",
                      ".rs", ".swift", ".dart", ".ex", ".exs", ".scala", ".vue", ".svelte", ".c", ".cpp", ".h")


def _mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…({len(value)} chars)"


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def fingerprint(rule: str, path: str, text: str) -> str:
    """Stable id of a finding: the same issue keeps its id when other lines of the file move."""
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha1(f"{rule}|{path}|{normalized}".encode("utf-8")).hexdigest()[:12]


def _finding(rule: str, category: str, severity: str, title: str, path: str, line: Optional[int], text: str,
             snippet: str, explanation: str, recommendation: str, confidence: float = 0.9) -> Dict[str, Any]:
    return {"fingerprint": fingerprint(rule, path, text), "rule": rule, "category": category, "severity": severity,
            "title": title, "file": path, "line": line, "snippet": snippet.strip()[:200],
            "explanation": explanation, "recommendation": recommendation, "source": "rule", "confidence": confidence}


# (rule, title, severity, pattern; group 1 = the secret)
_KNOWN_SECRETS: List[Tuple[str, str, str, re.Pattern]] = [
    ("SEC-A1-PRIVATE-KEY", "Private key committed", "critical",
     re.compile(r"(-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----)")),
    ("SEC-A1-AWS-KEY", "AWS access key", "critical", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")),
    ("SEC-A1-GITHUB-TOKEN", "GitHub token", "critical",
     re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("SEC-A1-SLACK-TOKEN", "Slack token", "high", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    ("SEC-A1-GOOGLE-KEY", "Google API key", "high", re.compile(r"\b(AIza[0-9A-Za-z_\-]{35})\b")),
    ("SEC-A1-STRIPE-KEY", "Stripe live secret key", "critical", re.compile(r"\b((?:sk|rk)_live_[0-9A-Za-z]{16,})\b")),
    ("SEC-A1-OPENAI-KEY", "AI service API key", "high",
     re.compile(r"\b(sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{32,}|gsk_[A-Za-z0-9]{40,})\b")),
    ("SEC-A1-AZURE-STORAGE", "Azure Storage account key", "critical",
     re.compile(r"AccountKey=([A-Za-z0-9+/=]{40,})", re.IGNORECASE)),
    ("SEC-A1-AZURE-SAS", "Azure SAS token", "high", re.compile(r"[?&]sig=([A-Za-z0-9%+/=]{30,})")),
    ("SEC-A1-JWT", "JSON Web Token", "high",
     re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")),
]
_CONNECTION_STRING = re.compile(
    r"\b((?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|mssql|sqlserver)://)"
    r"([^:@/\s'\"]+):([^@/\s'\"]+)@", re.IGNORECASE)
_PASSWORD_IN_CONN = re.compile(r"\b(?:Password|Pwd)\s*=\s*([^;'\"\s]{4,})", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"([A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?key|auth[_-]?token|"
    r"access[_-]?token|token|client[_-]?secret|private[_-]?key|signing[_-]?key|encryption[_-]?key)[A-Za-z0-9_]*)"
    r"[\"']?\s*(?::=|=|:|=>)\s*[\"'`]([^\"'`\s]{6,})[\"'`]", re.IGNORECASE)
_SECRET_NAME = r"([A-Za-z0-9_]*(?:PASSWORD|SECRET|KEY|TOKEN|PWD)[A-Za-z0-9_]*)"
_ENV_DEFAULTS = [   # group 1 = variable name, group 2 = the default value
    re.compile(r"(?:os\.getenv|os\.environ\.get|getenv|ENV\.fetch|System\.getenv|Environment\.GetEnvironmentVariable)"
               r"\s*\(\s*[\"']" + _SECRET_NAME + r"[\"']\s*,\s*[\"']([^\"']{6,})[\"']", re.IGNORECASE),
    re.compile(r"(?:os\.getenv|os\.environ\.get)\s*\(\s*[\"']" + _SECRET_NAME + r"[\"']\s*\)\s*or\s*[\"']([^\"']{6,})[\"']",
               re.IGNORECASE),
    re.compile(r"process\.env\.?\[?[\"']?" + _SECRET_NAME + r"[\"']?\]?\s*(?:\|\||\?\?)\s*[\"'`]([^\"'`]{6,})[\"'`]",
               re.IGNORECASE),
]
_URL = re.compile(r"\b(https?)://([A-Za-z0-9.\-]+)(:\d+)?[^\s'\"`)<>]*", re.IGNORECASE)
_IP = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
_LOCAL_HOSTS = re.compile(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|::1|[a-z0-9-]+\.local|host\.docker\.internal|"
                          r"example\.(com|org|net)|[a-z0-9.-]*\.example\.(com|org|net)|schemas?\.[a-z0-9.-]+|"
                          r"www\.w3\.org|json-schema\.org)$", re.IGNORECASE)
_ENV_FILE = re.compile(r"(^|/)\.env(\.[A-Za-z0-9_-]+)?$", re.IGNORECASE)
_ENV_TEMPLATE = re.compile(r"\.(example|sample|template|dist|defaults?)$", re.IGNORECASE)
_KEY_FILE = re.compile(r"(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)$|\.(pem|key|pfx|p12|jks|keystore|ppk)$|"
                       r"(^|/)(credentials|service[-_]?account[^/]*)\.json$", re.IGNORECASE)


def _is_comment_or_doc(path: str) -> bool:
    return path.lower().endswith(_DOC_EXTENSIONS)


def _scan_secrets(path: str, line: int, text: str, seen: set) -> Iterable[Dict[str, Any]]:
    for rule, title, severity, pattern in _KNOWN_SECRETS:
        for match in pattern.finditer(text):
            secret = match.group(1)
            if rule != "SEC-A1-PRIVATE-KEY" and _PLACEHOLDER.search(secret):
                continue
            seen.add(match.span(1))
            snippet = text.replace(secret, _mask(secret)) if rule != "SEC-A1-PRIVATE-KEY" else text
            yield _finding(rule, "Hard-coded secret", severity, title, path, line, text, snippet,
                           "A credential in the code is visible to everyone with access to the repository and stays "
                           "in its history even after it is removed.",
                           "Remove it, rotate (revoke) the credential, and load it from a secret store or an "
                           "environment variable instead.")
    for match in _CONNECTION_STRING.finditer(text):
        password = match.group(3)
        if _PLACEHOLDER.search(password) or password.startswith(("$", "%", "{")):
            continue
        seen.add(match.span(3))
        yield _finding("SEC-A1-CONNECTION-STRING", "Hard-coded secret", "high", "Password in a connection string",
                       path, line, text, text.replace(password, _mask(password)),
                       "The database/broker password is part of a connection string in the code.",
                       "Read the connection string (or at least the password) from configuration or a secret store.")
    for match in _PASSWORD_IN_CONN.finditer(text):
        password = match.group(1)
        if _PLACEHOLDER.search(password) or password.startswith(("$", "%", "{")) or match.span(1) in seen:
            continue
        seen.add(match.span(1))
        yield _finding("SEC-A1-CONNECTION-STRING", "Hard-coded secret", "high", "Password in a connection string",
                       path, line, text, text.replace(password, _mask(password)),
                       "The connection string contains a password.",
                       "Read the connection string (or at least the password) from configuration or a secret store.")
    for match in _SECRET_ASSIGNMENT.finditer(text):
        name, value = match.group(1), match.group(2)
        if (match.span(2) in seen or _PLACEHOLDER.search(value) or value.startswith(("$", "%(", "{", "process.env"))
                or re.fullmatch(r"[A-Z0-9_]+", value) or re.fullmatch(r"[a-z]+(\.[a-z]+)+", value)
                or re.fullmatch(r"[a-z]+([_-][a-z]+)+|[a-z]+([A-Z][a-z]+)+", value)     # key names: "auth_token"
                or "/" in value and " " not in value and value.count("/") > 2):
            continue
        strong = len(value) >= 16 and _entropy(value) >= 3.5
        seen.add(match.span(2))
        yield _finding("SEC-A1-GENERIC-SECRET", "Hard-coded secret", "high" if strong else "medium",
                       f"Hard-coded value for `{name[:40]}`", path, line, text, text.replace(value, _mask(value)),
                       "A secret-looking value is assigned directly in the code.",
                       "Load it from an environment variable or secret store; keep only a placeholder in examples.",
                       confidence=0.85 if strong else 0.6)


def _scan_env_defaults(path: str, line: int, text: str, seen: set) -> Iterable[Dict[str, Any]]:
    for match in (m for pattern in _ENV_DEFAULTS for m in pattern.finditer(text)):
        name, value = match.group(1), match.group(2)
        if _PLACEHOLDER.search(value) or match.span(2) in seen:
            continue
        seen.add(match.span(2))
        yield _finding("SEC-A2-ENV-DEFAULT", "Environment variable leakage", "high",
                       f"Secret used as the default of `{name}`", path, line, text, text.replace(value, _mask(value)),
                       "When the environment variable is missing, the real secret in the code is used, so the secret "
                       "is in the repository anyway.",
                       "Remove the default and fail clearly when the variable is not set.")


def _scan_config(path: str, line: int, text: str) -> Iterable[Dict[str, Any]]:
    """Plain HTTP anywhere in code or config; hard-coded URLs / IPs only in source files (config is the right place)."""
    if _TEST_PATH.search(path):
        return
    source = path.lower().endswith(_SOURCE_EXTENSIONS) and not _CONFIG_PATH.search(path)
    stripped = text.strip()
    if stripped.startswith(("#", "//", "*", "/*", "<!--", "import ", "from ", "require(", "@see", "--")):
        return
    for match in _URL.finditer(text):
        scheme, host = match.group(1).lower(), match.group(2)
        if _LOCAL_HOSTS.match(host) or (scheme == "https" and not source):
            continue
        if scheme == "http":
            yield _finding("SEC-A5-INSECURE-HTTP", "Insecure transport", "medium", "Unencrypted HTTP URL", path, line,
                           text, text, f"`{host}` is called over plain HTTP, so the traffic can be read or changed.",
                           "Use HTTPS, and keep the URL in configuration.", confidence=0.8)
        else:
            yield _finding("SEC-A3-HARDCODED-URL", "Hard-coded configuration", "low",
                           "Hard-coded service URL", path, line, text, text,
                           f"`{host}` is written into the code, so changing environments needs a code change.",
                           "Move the URL to the stack's configuration/constants file or an environment variable.",
                           confidence=0.7)
    if not source:
        return
    for match in _IP.finditer(text):
        ip = match.group(1)
        parts = [int(p) for p in ip.split(".")]
        if any(p > 255 for p in parts) or ip.startswith(("127.", "0.")) or ip == "255.255.255.255":
            continue
        if re.search(r"(version|v)\s*[:=]?\s*[\"']?" + re.escape(ip), text, re.IGNORECASE):
            continue
        yield _finding("SEC-A3-HARDCODED-IP", "Hard-coded configuration", "low", "Hard-coded IP address", path, line,
                       text, text, f"The address `{ip}` is written into the code.",
                       "Move it to configuration or an environment variable.", confidence=0.6)


def scan_file(path: str, change_type: str, added: Dict[int, str]) -> List[Dict[str, Any]]:
    """Findings for one changed file, from its added lines only (existing code is not re-reported)."""
    findings: List[Dict[str, Any]] = []
    name = path.rsplit("/", 1)[-1]
    if _ENV_FILE.search(path) and not _ENV_TEMPLATE.search(name):
        findings.append(_finding("SEC-A2-ENV-FILE", "Environment variable leakage", "high",
                                 f"Environment file `{name}` committed", path, None, path, path,
                                 "Environment files usually hold real credentials and are meant to stay on each "
                                 "machine.",
                                 "Remove the file from the PR, add it to .gitignore, rotate any secret it contained, "
                                 "and commit a `.env.example` with placeholders instead."))
    elif _KEY_FILE.search(path) and "add" in change_type:
        findings.append(_finding("SEC-A2-KEY-FILE", "Hard-coded secret", "high", f"Key or credential file `{name}`",
                                 path, None, path, path,
                                 "Private keys, certificates with keys and credential files must not be in the "
                                 "repository.",
                                 "Remove the file, rotate the key, and load it from a secret store at runtime."))
    if name == ".gitignore":
        for line, text in added.items():
            if re.match(r"^\s*!\s*\.env(\s|$)", text):
                findings.append(_finding("SEC-A2-GITIGNORE", "Environment variable leakage", "medium",
                                         "`.gitignore` re-includes .env", path, line, text, text,
                                         "This rule makes Git track the .env file.",
                                         "Remove the rule so .env files stay untracked."))
    if _is_comment_or_doc(path):
        return findings
    test_file = bool(_TEST_PATH.search(path))
    for line, text in sorted(added.items()):
        if len(text) > 2000:          # minified / generated line
            continue
        seen: set = set()
        line_findings = list(_scan_secrets(path, line, text, seen)) + list(_scan_env_defaults(path, line, text, seen))
        if test_file:                 # test fixtures: only clear key formats, at lower severity
            line_findings = [dict(f, severity="medium") for f in line_findings if f["rule"] != "SEC-A1-GENERIC-SECRET"]
        findings += line_findings + list(_scan_config(path, line, text))
    return findings


def scan_files(files: Iterable[Any]) -> List[Dict[str, Any]]:
    """Findings for every scanned file of a PullRequestDiff (objects with path / change_type / added / skipped)."""
    findings: List[Dict[str, Any]] = []
    for changed in files:
        if getattr(changed, "skipped", None):
            continue
        findings += scan_file(changed.path, changed.change_type, changed.added)
    return dedupe(findings)


def dedupe(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result, seen = [], set()
    for finding in sort_findings(findings):
        if finding["fingerprint"] in seen:
            continue
        seen.add(finding["fingerprint"])
        result.append(finding)
    return result


def sort_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    key: Callable[[Dict[str, Any]], Any] = lambda f: (SEVERITY_ORDER.get(f.get("severity"), 9), f.get("file") or "",
                                                      f.get("line") or 0)
    return sorted(findings, key=key)


def counts(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    result = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in findings:
        result[finding.get("severity", "low")] = result.get(finding.get("severity", "low"), 0) + 1
    return result
