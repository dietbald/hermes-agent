from pathlib import Path

from agent.skill_utils import find_skill_name_collisions


def _skill(root: Path, folder: str, name: str, body: str) -> Path:
    path = root / folder
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_reports_different_content_same_name_and_preserves_precedence(tmp_path):
    local = tmp_path / "local"
    external = tmp_path / "external"
    first = _skill(local, "old", "hotel-price-research", "old recipe")
    second = _skill(external, "new", "hotel-price-research", "new recipe")

    collisions = find_skill_name_collisions([local, external])

    assert len(collisions) == 1
    assert collisions[0]["name"] == "hotel-price-research"
    assert collisions[0]["selected_path"] == str(first / "SKILL.md").removesuffix("/SKILL.md")
    assert collisions[0]["entries"][1]["path"] == str(second)


def test_ignores_symlink_alias_and_identical_mirror(tmp_path):
    local = tmp_path / "local"
    external = tmp_path / "external"
    source = _skill(external, "canonical", "hotel-price-research", "same recipe")
    local.mkdir(parents=True)
    (local / "alias").symlink_to(source, target_is_directory=True)
    _skill(external, "mirror", "hotel-price-research", "same recipe")

    assert find_skill_name_collisions([local, external]) == []
