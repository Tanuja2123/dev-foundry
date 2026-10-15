"""
Online project initializers: official generators served over HTTPS that return a project zip.

They need no local toolchain (no JDK, Maven or Gradle on the DevFoundry server) and answer in about a second:
- spring-boot : Spring Initializr  (https://start.spring.io)       Java / Kotlin / Groovy, Maven / Gradle
- quarkus     : Quarkus            (https://code.quarkus.io)       Java / Kotlin, Maven / Gradle
- micronaut   : Micronaut Launch   (https://launch.micronaut.io)   Java / Kotlin / Groovy, Gradle / Maven

The agent passes structured options (validated here); the zip is downloaded from an allow-listed host only and
extracted into the requested folder with zip-slip, size and file-count protection. The request URL is recorded so
the output is reproducible ("GET https://start.spring.io/starter.zip?...").
"""
import difflib
import io
import os
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.core.logger import logger
from app.services.mcp.ado_git_push import AdoPushError, MAX_FILES

ALLOWED_HOSTS = {"start.spring.io", "code.quarkus.io", "launch.micronaut.io"}
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_ZIP_BYTES = 25_000_000
MAX_EXTRACTED_BYTES = 50_000_000

_IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_LIST = re.compile(r"^[A-Za-z0-9_.:,-]{0,2000}$")
_SAFE_FOLDER = re.compile(r"^[A-Za-z0-9._/-]+$")

# Tests replace this to use httpx.MockTransport.
transport_factory: Callable[[], Optional[httpx.BaseTransport]] = lambda: None


@dataclass
class InitializerRun:
    name: str
    folder: str
    url: str          # full request URL (reproducible)
    version: str      # e.g. "Spring Boot 4.1.1"

    def to_dict(self) -> Dict[str, str]:
        return {"command": f"GET {self.url}", "cwd": self.folder, "initializer": self.name, "version": self.version}


def _error(message: str, error_type: str = "INITIALIZER_FAILED", details: str = "") -> AdoPushError:
    return AdoPushError(message, error_type=error_type, details=details[-1500:])


def _client() -> httpx.Client:
    transport = transport_factory()
    kwargs: Dict[str, Any] = {"timeout": REQUEST_TIMEOUT_SECONDS, "follow_redirects": False,
                              "headers": {"User-Agent": "DevFoundry-scaffolder"}}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def _opt(options: Mapping[str, Any], key: str, default: str = "", pattern: re.Pattern = _IDENT) -> str:
    value = str(options.get(key, default) or default).strip()
    if value and not pattern.match(value):
        raise _error(f"Initializer option '{key}' has an invalid value '{value}'.", "VALIDATION_ERROR")
    return value


def _csv(options: Mapping[str, Any], key: str) -> List[str]:
    raw = options.get(key) or []
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    items = [str(i).strip() for i in items if str(i).strip()]
    _opt({key: ",".join(items)}, key, pattern=_LIST)
    return items


def _choice(options: Mapping[str, Any], key: str, mapping: Dict[str, str], default: str) -> str:
    value = str(options.get(key) or default).strip().lower()
    if value not in mapping:
        raise _error(f"Initializer option '{key}' must be one of: {', '.join(mapping)} (got '{value}').",
                     "VALIDATION_ERROR")
    return mapping[value]


def _package_from(group: str, artifact: str) -> str:
    return f"{group}.{re.sub(r'[^A-Za-z0-9_]', '', artifact).lower() or 'app'}"


# --------------------------------------------------------------------------- request builders

def _spring(options: Mapping[str, Any], default_name: str) -> Tuple[str, Dict[str, Any], str]:
    group = _opt(options, "group", "com.example")
    artifact = _opt(options, "artifact", default_name)
    params = {
        "type": _choice(options, "build", {"maven": "maven-project", "gradle": "gradle-project",
                                           "gradle-kotlin": "gradle-project-kotlin"}, "maven"),
        "language": _choice(options, "language", {"java": "java", "kotlin": "kotlin", "groovy": "groovy"}, "java"),
        "packaging": _choice(options, "packaging", {"jar": "jar", "war": "war"}, "jar"),
        "groupId": group, "artifactId": artifact, "name": _opt(options, "name", artifact),
        "packageName": _opt(options, "package", _package_from(group, artifact)),
        "dependencies": ",".join(_csv(options, "dependencies")),
    }
    for key, param in (("java_version", "javaVersion"), ("boot_version", "bootVersion")):
        value = _opt(options, key)
        if value:
            params[param] = value
    return "https://start.spring.io/starter.zip", params, "Spring Boot"


def _quarkus(options: Mapping[str, Any], default_name: str) -> Tuple[str, Dict[str, Any], str]:
    extensions = [e if ":" in e else f"io.quarkus:quarkus-{e}" for e in _csv(options, "extensions")]
    params: Dict[str, Any] = {
        "g": _opt(options, "group", "org.acme"), "a": _opt(options, "artifact", default_name),
        "b": _choice(options, "build", {"maven": "MAVEN", "gradle": "GRADLE", "gradle-kotlin": "GRADLE_KOTLIN_DSL"},
                     "maven"),
        "cn": "code.quarkus.io",
    }
    if extensions:
        params["e"] = extensions
    java = _opt(options, "java_version")
    if java:
        params["j"] = java
    return "https://code.quarkus.io/d", params, "Quarkus"


def _micronaut(options: Mapping[str, Any], default_name: str) -> Tuple[str, Dict[str, Any], str]:
    app_type = _choice(options, "type", {"app": "default", "default": "default", "cli": "cli", "function": "function",
                                         "grpc": "grpc", "messaging": "messaging"}, "app")
    group = _opt(options, "group", "com.example")
    artifact = re.sub(r"[^a-z0-9-]", "", _opt(options, "artifact", default_name).lower()) or "app"
    params: Dict[str, Any] = {
        "lang": _choice(options, "language", {"java": "JAVA", "kotlin": "KOTLIN", "groovy": "GROOVY"}, "java"),
        "build": _choice(options, "build", {"gradle": "GRADLE", "gradle-kotlin": "GRADLE_KOTLIN", "maven": "MAVEN"},
                         "gradle"),
        "test": _choice(options, "test", {"junit": "JUNIT", "spock": "SPOCK", "kotest": "KOTEST"}, "junit"),
    }
    features = _csv(options, "features")
    if features:
        params["features"] = features
    java = _opt(options, "java_version")
    if java:
        params["javaVersion"] = java if java.upper().startswith("JDK_") else f"JDK_{java}"
    return f"https://launch.micronaut.io/create/{app_type}/{group}.{artifact}", params, "Micronaut"


INITIALIZERS: Dict[str, Dict[str, Any]] = {
    "spring-boot": {
        "title": "Spring Boot via Spring Initializr (Java / Kotlin / Groovy; Maven or Gradle)",
        "options": "build=maven|gradle|gradle-kotlin, language=java|kotlin|groovy, java_version (e.g. 21), "
                   "boot_version (omit for the latest stable), group, artifact, package, packaging=jar|war, "
                   "dependencies (Initializr ids, e.g. web,data-jpa,postgresql,security,validation,actuator,lombok)",
        "build": _spring,
        "metadata": "https://start.spring.io/metadata/client",
    },
    "quarkus": {
        "title": "Quarkus via code.quarkus.io (Java / Kotlin; Maven or Gradle)",
        "options": "build=maven|gradle|gradle-kotlin, java_version, group, artifact, "
                   "extensions (e.g. rest,rest-jackson,hibernate-orm-panache,jdbc-postgresql)",
        "build": _quarkus,
        "metadata": "https://code.quarkus.io/api/extensions?platformOnly=true",
    },
    "micronaut": {
        "title": "Micronaut via launch.micronaut.io (Java / Kotlin / Groovy; Gradle or Maven)",
        "options": "type=app|cli|function|grpc|messaging, language=java|kotlin|groovy, build=gradle|gradle-kotlin|maven, "
                   "test=junit|spock|kotest, java_version, group, artifact, features (e.g. data-jdbc,postgres,security-jwt)",
        "build": _micronaut,
        "metadata": "https://launch.micronaut.io/application-types/default/features",
    },
}


def describe_initializers() -> List[Dict[str, str]]:
    return [{"name": name, "title": spec["title"], "options": spec["options"]} for name, spec in INITIALIZERS.items()]


# --------------------------------------------------------------------------- download + extract

def _valid_ids(name: str) -> List[str]:
    """Known dependency / extension / feature ids (only fetched to suggest a fix after the service rejected one)."""
    try:
        with _client() as client:
            data = client.get(INITIALIZERS[name]["metadata"],
                              headers={"Accept": "application/vnd.initializr.v2.2+json"}).json()
    except Exception:
        return []
    if name == "spring-boot":
        return [d["id"] for group in data.get("dependencies", {}).get("values", []) for d in group.get("values", [])]
    if name == "quarkus":
        return [e["id"].split(":")[-1].replace("quarkus-", "") for e in data if isinstance(e, dict) and "id" in e]
    return [f.get("name") for f in data.get("features", []) if f.get("name")]


def _suggest(name: str, message: str) -> str:
    unknown = re.findall(r"'([^']+)'", message)
    known = _valid_ids(name)
    hints = []
    for item in unknown:
        close = difflib.get_close_matches(item.split(":")[-1].replace("quarkus-", ""), known, n=4, cutoff=0.5)
        if close:
            hints.append(f"'{item}' -> did you mean: {', '.join(close)}?")
    return " ".join(hints)


def extract_zip(data: bytes, destination: str) -> int:
    """
    Extracts a project zip into `destination`, dropping the single top-level folder initializers wrap projects in.
    Rejects absolute paths, '..', too many files and zip bombs. Returns the number of files written.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise _error(f"The initializer did not return a valid zip ({e}).")
    entries = [i for i in archive.infolist() if not i.is_dir()]
    names = [i.filename.replace("\\", "/") for i in entries]
    tops = {n.split("/", 1)[0] for n in names}
    strip = len(tops) == 1 and all("/" in n for n in names)
    if len(entries) > MAX_FILES:
        raise _error(f"The initializer returned {len(entries)} files; the limit is {MAX_FILES}.", "VALIDATION_ERROR")
    if sum(i.file_size for i in entries) > MAX_EXTRACTED_BYTES:
        raise _error("The initializer zip is too large once extracted.", "VALIDATION_ERROR")
    root = os.path.realpath(destination)
    written = 0
    for info, name in zip(entries, names):
        rel = name.split("/", 1)[1] if strip else name
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            raise _error(f"Unsafe path '{name}' in the initializer zip.", "VALIDATION_ERROR")
        target = os.path.realpath(os.path.join(root, *parts))
        if os.path.commonpath([root, target]) != root:
            raise _error(f"Unsafe path '{name}' in the initializer zip.", "VALIDATION_ERROR")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(info) as source, open(target, "wb") as out:
            out.write(source.read(MAX_EXTRACTED_BYTES + 1))
        written += 1
    return written


def _resolved_version(name: str, response: httpx.Response, params: Dict[str, Any]) -> str:
    if name == "spring-boot":
        boot = params.get("bootVersion")
        if not boot:
            try:
                with _client() as client:
                    boot = client.get(INITIALIZERS[name]["metadata"],
                                      headers={"Accept": "application/vnd.initializr.v2.2+json"}
                                      ).json()["bootVersion"]["default"]
            except Exception:
                boot = "latest"
        return f"Spring Boot {boot}"
    return {"quarkus": "Quarkus (latest platform)", "micronaut": "Micronaut (latest)"}[name]


def run_initializer(spec: Mapping[str, Any], workspace: str, default_name: str) -> InitializerRun:
    """Validates one initializer request, downloads the project zip and extracts it into workspace/<folder>."""
    name = str(spec.get("name") or "").strip().lower()
    if name not in INITIALIZERS:
        raise _error(f"Unknown initializer '{name}'. Available: {', '.join(INITIALIZERS)}.", "VALIDATION_ERROR")
    folder = str(spec.get("folder") or ".").strip().replace("\\", "/").strip("/") or "."
    if folder != "." and (not _SAFE_FOLDER.match(folder) or ".." in folder.split("/")):
        raise _error(f"Folder '{folder}' must be a relative folder inside the repository.", "VALIDATION_ERROR")
    options = spec.get("options") or {}
    if not isinstance(options, Mapping):
        raise _error("Initializer options must be an object of name/value pairs.", "VALIDATION_ERROR")
    safe_default = re.sub(r"[^A-Za-z0-9_.-]", "-", default_name).strip("-") or "app"
    url, params, label = INITIALIZERS[name]["build"](options, safe_default)
    host = httpx.URL(url).host
    if host not in ALLOWED_HOSTS or httpx.URL(url).scheme != "https":
        raise _error(f"Host '{host}' is not an allowed initializer host.", "VALIDATION_ERROR")
    full_url = f"{url}?{urlencode(params, doseq=True)}"

    started = time.monotonic()
    logger.info(f"[Scaffold] Initializer {name}: GET {full_url}")
    try:
        with _client() as client:
            response = client.get(url, params=params)
    except httpx.HTTPError as e:
        raise _error(f"{label} initializer ({host}) could not be reached: {type(e).__name__}. Try again later.",
                     "INITIALIZER_UNAVAILABLE")
    if response.status_code >= 500:
        raise _error(f"{label} initializer ({host}) is unavailable (HTTP {response.status_code}). Try again later.",
                     "INITIALIZER_UNAVAILABLE")
    if response.status_code >= 400:
        message = response.text[:500]
        try:
            message = response.json().get("message") or message
        except Exception:
            pass
        raise _error(f"{label} initializer rejected the options: {message}", "VALIDATION_ERROR",
                     _suggest(name, message))
    if len(response.content) > MAX_ZIP_BYTES:
        raise _error("The initializer response is too large.", "VALIDATION_ERROR")

    destination = os.path.join(workspace, *([] if folder == "." else folder.split("/")))
    os.makedirs(destination, exist_ok=True)
    count = extract_zip(response.content, destination)
    version = _resolved_version(name, response, params)
    logger.info(f"[Scaffold] {label} initializer wrote {count} files into /{'' if folder == '.' else folder} "
                f"in {time.monotonic() - started:.1f}s ({version})")
    return InitializerRun(name=name, folder=folder, url=full_url, version=version)
