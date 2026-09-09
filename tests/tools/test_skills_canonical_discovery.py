"""Real filesystem contracts for installed version storage and public aliases."""
import json
from pathlib import Path

import pytest

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from agent.skill_utils import is_excluded_skill_path, iter_skill_index_files
from tools import skills_tool  # registers the public tools
from tools.registry import registry


def _skill(path, name):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Public workflow\n---\nFollow this workflow.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def skill_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    root = home / "skills"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    skills_tool._SKILLS_CACHE.clear()
    return home, root


@pytest.mark.parametrize("storage", [".workflow.versions/revision", ".workflow.pointer-token", ".workflow.stage.12.rollback-recovery.34", "category/.hidden/nested"])
def test_hidden_storage_is_not_a_skill_namespace(skill_home, storage):
    home, root = skill_home
    public = _skill(root / "workflow", "workflow")
    hidden = _skill(root / storage / "workflow", "workflow")
    hidden_only = _skill(root / storage / "hidden-only", "hidden-only")
    (hidden.parent / "legacy.md").write_text("Hidden legacy instructions", encoding="utf-8")
    (hidden.parent / "DESCRIPTION.md").write_text("---\ndescription: Hidden category\n---\n", encoding="utf-8")

    assert list(iter_skill_index_files(root, "SKILL.md")) == [public / "SKILL.md"]
    assert is_excluded_skill_path(hidden / "SKILL.md", root=root)
    assert not is_excluded_skill_path(public / "SKILL.md", root=root)
    listed = json.loads(registry.dispatch("skills_list", {}))
    assert [s["name"] for s in listed["skills"]] == ["workflow"]
    prompt = build_skills_system_prompt(skills_dir_override=root)
    assert prompt.count("    - workflow:") == 1
    assert "Hidden category" not in prompt
    assert "hidden-only" not in prompt
    snapshot = json.loads((home / ".skills_prompt_snapshot.json").read_text(encoding="utf-8"))
    assert set(snapshot["manifest"]) == {str((public / "SKILL.md").relative_to(root))}
    # Replay a pre-fix snapshot: hidden files used to participate in its manifest
    # and entries. A fresh session must rebuild rather than inject that catalog.
    for path in (hidden / "SKILL.md", hidden_only / "SKILL.md", hidden.parent / "DESCRIPTION.md"):
        stat = path.stat()
        snapshot["manifest"][str(path.relative_to(root))] = [stat.st_mtime_ns, stat.st_size]
    snapshot["skills"].append({"skill_name": "hidden-only", "category": storage})
    (home / ".skills_prompt_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    clear_skills_system_prompt_cache()
    assert build_skills_system_prompt(skills_dir_override=root) == prompt
    assert json.loads(registry.dispatch("skill_view", {"name": "workflow"}))["success"]
    for name in ("hidden-only", str(hidden_only.relative_to(root)), "legacy", f"{storage}/legacy"):
        result = json.loads(registry.dispatch("skill_view", {"name": name}))
        assert result["success"] is False, (name, result)


@pytest.mark.parametrize("external", [False, True])
def test_public_aliases_share_canonical_identity_without_losing_lookup(skill_home, tmp_path, external):
    home, root = skill_home
    storage = (tmp_path / "external" if external else root) / ".workflow.versions"
    target = _skill(storage / "revision", "workflow")
    _skill(storage / "old-revision", "workflow")
    public = root / "a-public" / "workflow"
    public.parent.mkdir()
    public.symlink_to(target, target_is_directory=True)
    alias = root / "z-alias" / "alternate"
    alias.parent.mkdir()
    alias.symlink_to(target, target_is_directory=True)
    # File aliases, not just directory aliases, refer to the same skill.
    file_alias = root / "file-alias"
    file_alias.mkdir()
    (file_alias / "SKILL.md").symlink_to(target / "SKILL.md")

    assert list(iter_skill_index_files(root, "SKILL.md")) == [public / "SKILL.md"]
    assert not is_excluded_skill_path(public / "SKILL.md", root=root)
    assert build_skills_system_prompt(skills_dir_override=root).count("    - workflow:") == 1
    listed = json.loads(registry.dispatch("skills_list", {}))
    assert [s["name"] for s in listed["skills"]] == ["workflow"]
    # Both bare directory aliases and qualified paths must survive index dedup.
    for name in ("workflow", "alternate", "a-public/workflow", "z-alias/alternate", "file-alias"):
        result = json.loads(registry.dispatch("skill_view", {"name": name}))
        assert result["success"], (name, result)
        assert result["content"] == (target / "SKILL.md").read_text(encoding="utf-8")

    references = target / "references"
    references.mkdir()
    (references / "guide.md").write_text("Public guide", encoding="utf-8")
    secret = tmp_path / "private.txt"
    secret.write_text("Private content", encoding="utf-8")
    (references / "escape.md").symlink_to(secret)
    good = json.loads(registry.dispatch("skill_view", {"name": "workflow", "file_path": "references/guide.md"}))
    bad = json.loads(registry.dispatch("skill_view", {"name": "workflow", "file_path": "references/escape.md"}))
    assert good["success"] and good["content"] == "Public guide"
    assert not bad["success"]

    # Directory and dangling-link cycles must terminate without duplicate skills.
    (target / "loop").symlink_to(target, target_is_directory=True)
    (root / "dangling").symlink_to(root / "missing", target_is_directory=True)
    assert list(iter_skill_index_files(root, "SKILL.md")) == [public / "SKILL.md"]
    other = _skill(root / "other", "workflow")
    clear_skills_system_prompt_cache(clear_snapshot=True)
    assert "workflow" in build_skills_system_prompt(skills_dir_override=root)
    collision = json.loads(registry.dispatch("skill_view", {"name": "workflow"}))
    assert not collision["success"] and "Ambiguous" in collision["error"]
    assert {str(Path(p).resolve()) for p in collision["matches"]} == {
        str(target / "SKILL.md"), str(other / "SKILL.md")}
