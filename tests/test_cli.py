from __future__ import annotations

import warnings
from argparse import ArgumentTypeError
from pathlib import Path

import pytest

from getpiccoma import __version__
from getpiccoma.cli import available_list, check_url, main, parse_args
from getpiccoma.piccoma import Entry, LoginError, NeedPurchase, Piccoma

VIEWER = "https://piccoma.com/web/viewer/8195/1185884"
NEXT = "https://piccoma.com/web/viewer/8195/1185887"
PRODUCT = "https://piccoma.com/web/product/8195/episodes"


class FakePiccoma:
    """Stands in for the client, answering `get` off a canned script."""

    instances: list[FakePiccoma] = []

    def __init__(self, script=None, entries=None, login_error=None):
        self.script = dict(script or {})
        self._entries = entries if entries is not None else []
        self.login_error = login_error
        self.gets = []
        self.logins = []
        self.entry_calls = []
        FakePiccoma.instances.append(self)

    is_valid_uri = staticmethod(Piccoma.is_valid_uri)
    parse_uri = staticmethod(Piccoma.parse_uri)

    def login(self, email, password):
        self.logins.append((email, password))
        if self.login_error is not None:
            raise self.login_error

    def entries(self, product_id, episode_type="E"):
        self.entry_calls.append((product_id, episode_type))
        return self._entries

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        save_path = kwargs.get("save_path", ".")
        action = self.script.get(url, (None, "title", True))
        if isinstance(action, Warning):
            warnings.warn(action.args[0], NeedPurchase, stacklevel=2)
            return None, Path(save_path), False
        next_url, name, saved = action
        return next_url, Path(save_path) / name, saved


@pytest.fixture
def client(monkeypatch):
    """Install a `FakePiccoma` factory in place of the real client."""
    FakePiccoma.instances.clear()

    def install(**kwargs):
        class Installed(FakePiccoma):
            def __init__(self):
                super().__init__(**kwargs)

        monkeypatch.setattr("getpiccoma.cli.Piccoma", Installed)

    return install


# --- argument parsing ---------------------------------------------------------


def test_check_url_accepts_a_viewer_url():
    assert check_url(VIEWER) == VIEWER


def test_check_url_accepts_a_product_url():
    assert check_url(PRODUCT) == PRODUCT


def test_check_url_rejects_anything_else():
    with pytest.raises(ArgumentTypeError, match="not a Piccoma url"):
        check_url("https://example.com/")


def test_available_list_names_both_url_forms():
    assert "/web/viewer/" in available_list()
    assert "/web/product/" in available_list()


def test_parse_args_defaults():
    parsed = parse_args([VIEWER])
    assert parsed.url == VIEWER
    assert parsed.savedir == "."
    assert not parsed.bulk
    assert not parsed.first
    assert not parsed.overwrite
    assert not parsed.metadata
    assert not parsed.volumes
    assert not parsed.quiet
    assert parsed.username is None


def test_parse_args_reads_the_flags():
    parsed = parse_args([VIEWER, "-b", "-d", "out", "-f", "-o", "-m", "-t", "-q", "-u", "me@example.com", "-p", "pw"])
    assert parsed.bulk
    assert parsed.savedir == "out"
    assert parsed.first
    assert parsed.overwrite
    assert parsed.metadata
    assert parsed.volumes
    assert parsed.quiet
    assert parsed.username == "me@example.com"
    assert parsed.password == "pw"


def test_version_flag_prints_the_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


# --- downloading --------------------------------------------------------------


def test_main_downloads_one_episode(client, capsys):
    client(script={VIEWER: (NEXT, "first", True)})
    main([VIEWER])

    piccoma = FakePiccoma.instances[0]
    assert [url for url, _ in piccoma.gets] == [VIEWER]
    out = capsys.readouterr().out
    assert f"get: {VIEWER}" in out
    assert "saved: first" in out
    assert "done." in out


def test_main_passes_the_flags_through(client):
    client(script={VIEWER: (None, "first", True)})
    main([VIEWER, "-d", "out", "-f", "-o", "-m"])

    _, kwargs = FakePiccoma.instances[0].gets[0]
    assert kwargs == {
        "save_path": "out",
        "overwrite": True,
        "only_first": True,
        "save_metadata": True,
        "print_log": True,
    }


def test_main_follows_every_episode_in_bulk(client):
    client(script={VIEWER: (NEXT, "first", True), NEXT: (None, "second", True)})
    main([VIEWER, "-b"])

    assert [url for url, _ in FakePiccoma.instances[0].gets] == [VIEWER, NEXT]


def test_main_stops_after_one_episode_without_bulk(client):
    client(script={VIEWER: (NEXT, "first", True), NEXT: (None, "second", True)})
    main([VIEWER])

    assert [url for url, _ in FakePiccoma.instances[0].gets] == [VIEWER]


def test_main_says_when_it_skipped_an_episode(client, capsys):
    client(script={VIEWER: (None, "first", False)})
    main([VIEWER])
    assert "skipped (already there): first" in capsys.readouterr().out


def test_main_stops_at_an_episode_it_may_not_read(client, capsys):
    client(script={VIEWER: (NEXT, "first", True), NEXT: NeedPurchase("second")})
    main([VIEWER, "-b"])

    assert [url for url, _ in FakePiccoma.instances[0].gets] == [VIEWER, NEXT]
    assert "needs a purchase" in capsys.readouterr().err


def test_main_is_quiet_when_told_to(client, capsys):
    client(script={VIEWER: (None, "first", True)})
    main([VIEWER, "-q"])
    assert capsys.readouterr().out == ""


# --- product urls -------------------------------------------------------------


def test_main_starts_a_product_url_at_its_first_episode(client, capsys):
    client(
        script={VIEWER: (NEXT, "first", True), NEXT: (None, "second", True)},
        entries=[Entry(id="1185884", title="one", url=VIEWER)],
    )
    main([PRODUCT])

    piccoma = FakePiccoma.instances[0]
    assert piccoma.entry_calls == [("8195", "E")]
    # A product url means the whole series, so `-b` is implied.
    assert [url for url, _ in piccoma.gets] == [VIEWER, NEXT]
    assert "list: 1 entries" in capsys.readouterr().out


def test_main_reads_the_volume_list_when_asked(client):
    client(
        script={VIEWER: (None, "first", True)},
        entries=[Entry(id="1185884", title="one", url=VIEWER)],
    )
    main([PRODUCT, "-t"])

    assert FakePiccoma.instances[0].entry_calls == [("8195", "V")]


def test_main_gives_up_on_an_empty_product(client, capsys):
    client(entries=[])
    with pytest.raises(SystemExit) as excinfo:
        main([PRODUCT])

    assert excinfo.value.code == 1
    assert "lists no episodes" in capsys.readouterr().err


# --- logging in ---------------------------------------------------------------


def test_main_logs_in_when_given_credentials(client, capsys):
    client(script={VIEWER: (None, "first", True)})
    main([VIEWER, "-u", "me@example.com", "-p", "pw"])

    assert FakePiccoma.instances[0].logins == [("me@example.com", "pw")]
    assert "logged in as: me@example.com" in capsys.readouterr().out


def test_main_prompts_for_a_password_it_was_not_given(client, monkeypatch):
    client(script={VIEWER: (None, "first", True)})
    monkeypatch.setattr("getpass.getpass", lambda *_: "typed")
    main([VIEWER, "-u", "me@example.com"])

    assert FakePiccoma.instances[0].logins == [("me@example.com", "typed")]


def test_main_exits_when_the_login_fails(client, capsys):
    client(login_error=LoginError("piccoma.com refused the credentials"))
    with pytest.raises(SystemExit) as excinfo:
        main([VIEWER, "-u", "me@example.com", "-p", "wrong"])

    assert excinfo.value.code == 1
    assert "error: piccoma.com refused" in capsys.readouterr().err


def test_main_warns_about_a_password_without_a_username(client, capsys):
    client(script={VIEWER: (None, "first", True)})
    main([VIEWER, "-p", "pw"])

    assert FakePiccoma.instances[0].logins == []
    assert "-p without -u does nothing" in capsys.readouterr().err
