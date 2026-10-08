import pytest

from app.services.mcp.ado_git_push import AdoPushError
from app.services.repo_structure import (apply_structure_update, files_to_map, normalize_structure,
                                         render_structure_tree, structure_folders)

BASE = [
    {"path": "/README.md", "content": "# app\n"},
    {"path": "/src/main.py", "content": '"""entry"""\n'},
    {"path": "/src/api/__init__.py", "content": '"""api"""\n'},
    {"path": "/docs/README.md", "content": "# docs\n"},
]


def test_normalize_structure_sorts_and_normalizes():
    result = normalize_structure([{"path": "src\\b.py", "content": "b"}, {"path": "a.md", "content": "a"}])
    assert result == [{"path": "/a.md", "content": "a"}, {"path": "/src/b.py", "content": "b"}]


@pytest.mark.parametrize("files, message", [
    ([{"path": "/a.txt", "content": "  "}], "empty content"),
    ([{"path": "/a/../b.txt", "content": "x"}], "'..'"),
    ([{"path": "/a.txt", "content": "x"}, {"path": "a.txt", "content": "y"}], "Duplicate"),
    ([{"path": "/a.txt"}], "must have string"),
    ([], "At least one file"),
])
def test_normalize_structure_rejects_invalid(files, message):
    with pytest.raises(AdoPushError, match=message):
        normalize_structure(files)


def test_files_to_map_accepts_models():
    class Model:
        def model_dump(self):
            return {"path": "/x.txt", "content": "x"}

    assert files_to_map([Model()]) == {"/x.txt": "x"}


def test_update_keeps_unmentioned_files_exactly():
    updated = apply_structure_update(BASE, upsert_files=[{"path": "/tests/.gitkeep", "content": "keep\n"}])
    by_path = {f["path"]: f["content"] for f in updated}
    for original in BASE:
        assert by_path[original["path"]] == original["content"]
    assert by_path["/tests/.gitkeep"] == "keep\n"


def test_update_replaces_and_removes_files_and_folders():
    updated = apply_structure_update(
        BASE,
        upsert_files=[{"path": "/README.md", "content": "# renamed\n"}],
        remove_paths=["/src/api/", "docs/README.md"])
    assert [f["path"] for f in updated] == ["/README.md", "/src/main.py"]
    assert updated[0]["content"] == "# renamed\n"


def test_rename_is_remove_plus_add():
    updated = apply_structure_update(BASE, upsert_files=[{"path": "/app/main.py", "content": '"""entry"""\n'}],
                                     remove_paths=["/src/main.py"])
    paths = [f["path"] for f in updated]
    assert "/app/main.py" in paths and "/src/main.py" not in paths


def test_update_rejects_unknown_remove_path_and_does_not_mutate_input():
    snapshot = [dict(f) for f in BASE]
    with pytest.raises(AdoPushError, match="no such file or folder"):
        apply_structure_update(BASE, remove_paths=["/nope"])
    assert BASE == snapshot


def test_update_validates_resulting_structure():
    with pytest.raises(AdoPushError, match="empty content"):
        apply_structure_update(BASE, upsert_files=[{"path": "/x.txt", "content": ""}])


def test_structure_folders():
    assert structure_folders(["/a/b/c.txt", "/a/d.txt", "/e.txt"]) == ["/a", "/a/b"]


def test_render_tree_is_deterministic_folders_first():
    tree = render_structure_tree(BASE, root_name="my-repo")
    assert tree == (
        "```text\n"
        "my-repo/\n"
        "├── docs/\n"
        "│   └── README.md\n"
        "├── src/\n"
        "│   ├── api/\n"
        "│   │   └── __init__.py\n"
        "│   └── main.py\n"
        "└── README.md\n"
        "```"
    )
    assert render_structure_tree(list(reversed(BASE)), root_name="my-repo") == tree


def test_rendered_tree_lists_every_stored_file():
    tree = render_structure_tree(BASE)
    for f in BASE:
        assert f["path"].rsplit("/", 1)[-1] in tree
