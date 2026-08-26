from ftre.services.workspace.service import ensure_workspace_ext_dir


def test_non_utf8_gitignore_is_preserved_and_updated(tmp_path):
    gitignore = tmp_path / ".gitignore"
    original = b"# GBK comment: \xb9\xa4\xd7\xf7\xc7\xf8\r\n"
    gitignore.write_bytes(original)

    ensure_workspace_ext_dir(str(tmp_path))

    assert (tmp_path / ".ftre" / "skills").is_dir()
    assert (tmp_path / ".ftre" / "mcp.json").is_file()
    updated = gitignore.read_bytes()
    assert updated.startswith(original)
    assert updated.endswith(b".ftre/\r\n")
