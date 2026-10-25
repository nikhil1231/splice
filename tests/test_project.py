"""What one archive is: splice.yaml, and how splice finds it.

The code used to know one family -- a media root under ~/Movies, four
speakers, Konkani in the schema. All of that now comes from the project, and
the thing worth pinning hardest is that moving it there changed nothing for
the archive it came from: the analysis fingerprint decides what is re-analysed,
and a changed one marks hours of hand-corrected transcripts stale.
"""

import hashlib
import json
from pathlib import Path

import pytest

from splice import analyze, backup, cli, config, schema
from splice.taxonomy import Taxonomy


def _no_project_hints(monkeypatch, tmp_path):
    monkeypatch.delenv("SPLICE_ROOT", raising=False)
    monkeypatch.delenv("SPLICE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


# --- finding it --------------------------------------------------------


def test_with_no_project_anywhere_splice_says_so_rather_than_guessing(tmp_path, monkeypatch):
    """A guessed folder is where a first run scatters raw/ and index/."""
    _no_project_hints(monkeypatch, tmp_path)
    with pytest.raises(config.NoProject, match="splice init"):
        config.media_root()


def test_a_splice_yaml_above_the_working_directory_is_the_project(tmp_path, monkeypatch):
    """The way git finds a repository: run it from anywhere inside."""
    _no_project_hints(monkeypatch, tmp_path)
    (tmp_path / "splice.yaml").write_text("name: Holiday\n")
    inside = tmp_path / "raw" / "phone"
    inside.mkdir(parents=True)
    monkeypatch.chdir(inside)
    assert config.media_root() == tmp_path
    assert config.event_name() == "Holiday"


def test_init_makes_a_project_and_the_default_and_never_overwrites_one(tmp_path, monkeypatch):
    _no_project_hints(monkeypatch, tmp_path)
    folder = tmp_path / "footage"
    cli.init(folder=folder, default=True)

    assert (folder / "raw").is_dir() and (folder / "index").is_dir()
    assert (folder / "taxonomy.yaml").is_file()
    assert config.media_root() == folder.resolve()
    assert config.project().name == "Footage"

    # Somebody's description of their family is not ours to reset.
    (folder / "splice.yaml").write_text("name: Mine\n")
    cli.init(folder=folder, default=True)
    assert (folder / "splice.yaml").read_text() == "name: Mine\n"


def test_a_misspelt_setting_is_refused_rather_than_ignored(monkeypatch, tmp_path):
    """`peoples:` silently ignored is a project about nobody, analysed for
    hours before anybody notices the speakers are all unknown."""
    path = tmp_path / "splice.yaml"
    path.write_text("peoples: []\n")
    monkeypatch.setenv("SPLICE_CONFIG", str(path))
    with pytest.raises(config.NoProject, match="peoples"):
        config.project()


def test_an_edit_to_splice_yaml_is_seen_without_a_restart(monkeypatch, tmp_path):
    """The server runs for an evening; the project is cached on mtime, not
    for the life of the process."""
    path = tmp_path / "splice.yaml"
    path.write_text("name: One\n")
    monkeypatch.setenv("SPLICE_CONFIG", str(path))
    assert config.event_name() == "One"
    path.write_text("name: Two\n")
    import os
    os.utime(path, ns=(1, 2_000_000_000_000_000_000))
    assert config.event_name() == "Two"


# --- the fingerprint ---------------------------------------------------


def test_the_cast_renders_the_schema_exactly_as_it_was_before_projects():
    """5d2e94204cff is the hash of the hand-written schema the archive was
    analysed with. The tests' cast is that archive's cast, so if this moves,
    every real clip analysed under it would be offered for re-analysis."""
    payload = json.dumps(schema.agy_schema(), sort_keys=True).encode()
    assert hashlib.sha256(payload).hexdigest()[:12] == "5d2e94204cff"


def test_a_projects_own_prompt_is_used_word_for_word(_own_project):
    """With no placeholders of its own it comes back unchanged -- which is
    how an archive keeps its fingerprint when its prompt moves out of the
    code and into the project."""
    prompts = _own_project / "prompts"
    prompts.mkdir()
    (prompts / "analyze.md").write_text("Watch `{video_path}` closely.\n{taxonomy}\n")

    built = analyze.build_prompt(Path("/raw/a.MOV"), Taxonomy.load())
    assert built.startswith("Watch `/raw/a.MOV` closely.")
    payload = ("Watch `{video_path}` closely.\n{taxonomy}\n"
               + json.dumps(schema.agy_schema(), sort_keys=True))
    assert analyze.prompt_sha() == hashlib.sha256(payload.encode()).hexdigest()[:12]


def test_changing_who_is_in_it_does_invalidate(_own_project):
    """The model is told who can speak, so a new person is a question it
    would answer differently."""
    before = analyze.prompt_sha()
    settings = _own_project / "splice.yaml"
    settings.write_text(settings.read_text() + "  - id: uncle\n")
    import os
    os.utime(settings, ns=(1, 2_000_000_000_000_000_000))
    assert analyze.prompt_sha() != before


# --- what the project reaches ------------------------------------------


def test_the_default_prompt_is_about_this_footage_and_these_people():
    built = analyze.build_prompt(Path("/raw/a.MOV"), Taxonomy.load())
    assert "Konkani-speaking grandparents in Goa" in built
    assert "verbatim** romanised Konkani" in built
    assert "`grandma` is the older woman." in built
    assert "Nikhil is the one holding the camera" in built
    for placeholder in ("{footage}", "{spoken}", "{written}", "{speakers}"):
        assert placeholder not in built


def test_a_project_about_nobody_attributes_every_line_to_nobody(monkeypatch, tmp_path):
    path = tmp_path / "splice.yaml"
    path.write_text("language: Spanish\n")
    monkeypatch.setenv("SPLICE_CONFIG", str(path))

    assert schema.speakers() == ()
    assert analyze._speaker("grandma") == ""
    built = analyze.build_prompt(Path("/raw/a.MOV"), Taxonomy.load())
    assert "verbatim** Spanish" in built
    assert "Konkani" not in built.replace("`konkani`", "")
    assert "always `unknown`" in built


def test_the_people_are_vocabulary_without_being_listed_twice(_own_project):
    (_own_project / "taxonomy.yaml").write_text("namespaces: [theme]\ntags: {}\n")
    vocabulary = Taxonomy.load()
    assert vocabulary.known("person:grandma")
    assert vocabulary.label("person:both") == "Both Grandparents"
    assert "person" in vocabulary.namespaces


def test_growing_the_default_vocabulary_gives_the_project_its_own_copy(_own_project):
    """The default is shared by every project; a promotion is one project's."""
    (_own_project / "taxonomy.yaml").unlink()
    vocabulary = Taxonomy.load()
    assert vocabulary.known("theme:money")

    vocabulary.promote("object:kite")
    own = (_own_project / "taxonomy.yaml").read_text()
    assert "object:kite" in own and "theme:money" in own
    assert "object:kite" not in config.default_taxonomy_path().read_text()


# --- the archive -------------------------------------------------------


def test_a_project_with_no_archive_backs_up_nowhere_and_says_so():
    with pytest.raises(backup.NoArchive, match="archive:"):
        backup.backup()


def test_an_archive_is_relative_to_the_media_root(_own_project, monkeypatch, tmp_path):
    settings = _own_project / "splice.yaml"
    settings.write_text(settings.read_text() + "archive: kept\n")
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    assert config.archive_dir() == tmp_path / "kept"


def test_an_archive_belongs_to_the_media_root_that_made_it(monkeypatch, tmp_path):
    """Mirroring deletes what the source lacks. A copied splice.yaml pointing
    a second project at the first one's archive would prune its transcripts."""
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "manifest.json").write_text(json.dumps({"media_root": "/somebody/else"}))
    monkeypatch.setenv("SPLICE_ARCHIVE", str(archive))
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path / "media"))
    config.index_dir().mkdir(parents=True)

    with pytest.raises(backup.Refused, match="is the archive of /somebody/else"):
        backup.backup()
    backup.backup(force=True)
    owner = json.loads((archive / "manifest.json").read_text())["media_root"]
    assert owner == str(tmp_path / "media")


def test_the_project_itself_is_backed_up_beside_the_transcripts(_own_project, monkeypatch, tmp_path):
    """splice.yaml and the vocabulary are hand-made and sit in the media root,
    where nothing else versions them."""
    archive = tmp_path / "archive"
    monkeypatch.setenv("SPLICE_ARCHIVE", str(archive))
    backup.backup()
    assert (archive / "project" / "splice.yaml").read_text() == (
        _own_project / "splice.yaml").read_text()
    assert (archive / "project" / "taxonomy.yaml").exists()
