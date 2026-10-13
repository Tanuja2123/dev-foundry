"""
Repository folder structure helpers.

A structure is a list of files ({"path": "/a/b.txt", "content": "..."}; binary files add "encoding": "base64").
Folders are never stored on their own: Git does not track empty folders, so a folder exists only because it
contains at least one file. The same stored list is rendered for the user and pushed to Azure DevOps, so what is
shown is what is pushed.

Files written by the agent must have content. Files produced by scaffold commands are kept exactly as generated,
including empty ones (e.g. the empty README.md of `uv init`), so structures are validated with allow_empty=True
once they contain generated files.
"""
from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.services.mcp.ado_git_push import AdoPushError, Base64Content, compute_changes, normalize_path, prepare_files


def _as_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return {}


def files_to_map(files: Iterable[Any]) -> Dict[str, str]:
    """Converts [{path, content}] (dicts or pydantic models) to a path -> content map, rejecting malformed items."""
    file_map: Dict[str, str] = {}
    for index, item in enumerate(files or []):
        data = _as_dict(item)
        path, content = data.get("path"), data.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise AdoPushError(f"File #{index + 1} must have string 'path' and 'content' fields.",
                               error_type="VALIDATION_ERROR")
        if path in file_map:
            raise AdoPushError(f"Duplicate file path '{path}'.", error_type="VALIDATION_ERROR")
        encoding = data.get("encoding")
        if encoding not in (None, "", "base64"):
            raise AdoPushError(f"File '{path}' has unsupported encoding '{encoding}'.", error_type="VALIDATION_ERROR")
        file_map[path] = Base64Content(content) if encoding == "base64" else content
    return file_map


def _as_list(prepared: Mapping[str, str]) -> List[Dict[str, str]]:
    result = []
    for path in sorted(prepared):
        content = prepared[path]
        item = {"path": path, "content": str(content)}
        if isinstance(content, Base64Content):
            item["encoding"] = "base64"
        result.append(item)
    return result


def normalize_structure(files: Iterable[Any], allow_empty: bool = False) -> List[Dict[str, str]]:
    """Validates a file list (paths, duplicates, content, limits) and returns it sorted by path."""
    return _as_list(prepare_files(files_to_map(files), allow_empty=allow_empty))


def apply_structure_update(current: List[Dict[str, str]], upsert_files: Optional[Iterable[Any]] = None,
                           remove_paths: Optional[Iterable[str]] = None) -> List[Dict[str, str]]:
    """
    Returns a new structure: `remove_paths` are removed first (a file path removes that file, a folder path removes
    every file under it), then `upsert_files` are added or replaced. Unmentioned files are kept exactly as they were
    (including empty or binary files a scaffold command generated); added/replaced files must have content.
    """
    files = files_to_map(current or [])

    for raw in remove_paths or []:
        target = normalize_path(str(raw).rstrip("/\\") or "/")
        if target in files:
            del files[target]
            continue
        under = [path for path in files if path.startswith(target + "/")]
        if not under:
            raise AdoPushError(f"Cannot remove '{raw}': no such file or folder in the current structure.",
                               error_type="VALIDATION_ERROR")
        for path in under:
            del files[path]

    for path, content in files_to_map(upsert_files or []).items():
        if not content.strip():
            raise AdoPushError(f"File '{path}' has empty content. Every file must contain text.",
                               error_type="VALIDATION_ERROR")
        files[normalize_path(path)] = content

    return _as_list(prepare_files(files, allow_empty=True))


def layer_changes(base: Optional[Iterable[Any]], structure: Iterable[Any]) -> Dict[str, List[str]]:
    """Paths added / edited / deleted in `structure` compared with `base` (e.g. the generator output)."""
    changes = compute_changes(files_to_map(base or []), files_to_map(structure or []))
    return {kind: [path for change_type, path, _ in changes if change_type == kind] for kind in ("add", "edit", "delete")}


def structure_folders(paths: Iterable[str]) -> List[str]:
    folders = set()
    for path in paths:
        parts = path.strip("/").split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            folders.add("/" + "/".join(parts[:depth]))
    return sorted(folders)


def render_structure_tree(files: Iterable[Mapping[str, str]], root_name: Optional[str] = None,
                          notes: Optional[Mapping[str, str]] = None) -> str:
    """
    Renders the structure as a deterministic tree (folders first, then files, alphabetical) in a Markdown code block.
    `notes` maps file paths to a short marker shown after the file name (e.g. "added").
    """
    notes = notes or {}
    paths = [item["path"] for item in files]
    tree: Dict[str, Any] = {}
    for path in paths:
        node = tree
        parts = path.strip("/").split("/")
        for folder in parts[:-1]:
            node = node.setdefault(folder + "/", {})
        node[parts[-1] + (f"  ({notes[path]})" if path in notes else "")] = None

    lines: List[str] = [f"{root_name}/" if root_name else "/"]

    def walk(node: Dict[str, Any], prefix: str) -> None:
        folders = sorted((k for k in node if node[k] is not None), key=str.lower)
        leaf_files = sorted((k for k in node if node[k] is None), key=str.lower)
        entries = folders + leaf_files
        for index, name in enumerate(entries):
            last = index == len(entries) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{name}")
            if node[name] is not None:
                walk(node[name], prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "```text\n" + "\n".join(lines) + "\n```"
