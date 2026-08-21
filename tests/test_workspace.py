import logging

from ftre.services.tools.builtin._workspace import ensure_workspace_ext_dir


def test_non_utf8_gitignore_does_not_block_workspace_setup(tmp_path, caplog):
    gitignore = tmp_path / ".gitignore"
    original = b"# GBK comment: \xb9\xa4\xd7\xf7\xc7\xf8\r\n"
    gitignore.write_bytes(original)

    with caplog.at_level(logging.WARNING, logger="ftre.tools._workspace"):
        ensure_workspace_ext_dir(str(tmp_path))

    assert (tmp_path / ".ftre" / "skills").is_dir()
    assert (tmp_path / ".ftre" / "mcp.json").is_file()
    assert gitignore.read_bytes() == original
    assert ".gitignore" in caplog.text
