import os

import pytest

from rkai.download import atomic_write
from rkai.errors import RkaiError
from rkai.files import move_directory, open_file, remove_tree


@pytest.mark.parametrize("action", ["move", "remove"])
def test_directory_mutation_refuses_symlink_parent(tmp_path, action):
    outside = tmp_path / "outside"
    tree = outside / "tree"
    tree.mkdir(parents=True)
    victim = tree / "victim"
    victim.write_bytes(b"preserve original")
    parent = tmp_path / "parent"
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RkaiError) as error:
        if action == "move":
            move_directory(parent / "tree", tmp_path / "destination")
        else:
            remove_tree(parent / "tree")
    assert error.value.code == "unsafe_path"
    assert victim.read_bytes() == b"preserve original"
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_writable_leaf_link_does_not_truncate_external_file(tmp_path, link_kind):
    source = tmp_path / "external"
    source.write_bytes(b"preserve original")
    destination = tmp_path / "destination"
    if link_kind == "symlink":
        destination.symlink_to(source)
    else:
        os.link(source, destination)
    with pytest.raises(RkaiError) as error:
        with open_file(destination, "wb") as stream:
            stream.write(b"overwrite")
    assert error.value.code == "unsafe_path"
    assert source.read_bytes() == b"preserve original"


def test_directory_replaced_between_traversal_and_open_is_rejected(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"preserve original")
    original_open = os.open
    replaced = False

    def swap(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "nested" and flags & os.O_DIRECTORY and not replaced:
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
            replaced = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(RkaiError):
        with open_file(nested / "victim", "wb") as stream:
            stream.write(b"overwrite")
    assert replaced
    assert victim.read_bytes() == b"preserve original"


def test_atomic_write_rejects_linked_temporary_file(tmp_path):
    victim = tmp_path / "external"
    victim.write_bytes(b"preserve original")
    (tmp_path / "record.json.tmp").symlink_to(victim)
    with pytest.raises(RkaiError):
        atomic_write(tmp_path / "record.json", b"overwrite")
    assert victim.read_bytes() == b"preserve original"
    assert not (tmp_path / "record.json").exists()
