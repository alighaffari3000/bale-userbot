"""Argument wiring and output shape of `python -m bale_userbot`.

Nothing here touches the network; the commands themselves are the thin part,
and what breaks is the plumbing around them — a flag that never reaches the
call, or a log line landing in the middle of JSON.
"""

import json
import logging
import sys

import pytest
from baleclient.enums import ChatType

from bale_userbot import cli


def parse(*argv: str):
    return cli.build_parser().parse_args(argv)


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("login",),
        ("whoami",),
        ("groups",),
        ("members", "1"),
        ("history", "1"),
        ("pins", "1"),
        ("link", "1"),
    ],
)
def test_every_command_is_reachable(argv):
    assert parse(*argv).command == argv[0]


def test_history_takes_a_date_range():
    args = parse("history", "55", "--since", "2026-04-26", "--until", "2026-04-28")
    assert (args.chat_id, args.since, args.until) == (55, "2026-04-26", "2026-04-28")
    assert args.chat_type == "group"


def test_history_chat_type_maps_to_the_enum():
    assert cli.CHAT_TYPES[
        parse("history", "1", "--chat-type", "channel").chat_type
    ] is (ChatType.CHANNEL)
    assert cli.CHAT_TYPES["supergroup"] is ChatType.SUPER_GROUP


def test_members_defaults_to_profiles_and_everyone():
    args = parse("members", "7")
    assert (args.admins, args.no_profiles, args.limit) == (False, False, None)


def test_a_missing_chat_id_is_an_error_not_a_default():
    with pytest.raises(SystemExit):
        parse("members")


# --- output ----------------------------------------------------------------


def test_json_output_is_parseable(capsys):
    cli._emit([{"user_id": 1, "name": "علی"}], True, ["ignored"])
    payload = json.loads(capsys.readouterr().out)
    # ensure_ascii=False, or every Persian name comes back as escapes.
    assert payload == [{"user_id": 1, "name": "علی"}]


def test_human_output_prints_the_prepared_lines(capsys):
    cli._emit([{"ignored": True}], False, ["one", "two"])
    assert capsys.readouterr().out.splitlines() == ["one", "two"]


def test_role_and_handle_formatting():
    assert cli._role({"is_owner": True, "is_admin": True}) == "owner"
    assert cli._role({"is_owner": False, "is_admin": True}) == "admin"
    assert cli._role({"is_owner": False, "is_admin": False}) == ""
    assert cli._handle("ali") == "@ali"
    assert cli._handle(None) == "-"


# --- dispatch --------------------------------------------------------------


async def _noop(*args, **kwargs) -> int:
    return 0


def test_data_commands_log_to_stderr(monkeypatch):
    # Their records go to stdout; a log line there would break `--json | jq`.
    streams = []
    monkeypatch.setattr(
        cli, "setup_logging", lambda level, stream=None: streams.append(stream)
    )
    monkeypatch.setitem(cli._COMMANDS, "groups", _noop)
    cli.main(["groups"])
    assert streams == [sys.stderr]


def test_login_keeps_logging_on_stdout(monkeypatch):
    streams = []
    monkeypatch.setattr(
        cli, "setup_logging", lambda level, stream=None: streams.append(stream)
    )
    monkeypatch.setattr(cli, "cmd_login", _noop)
    cli.main(["login"])
    assert streams == [None]


def test_the_session_flag_overrides_the_config(monkeypatch, tmp_path):
    seen = {}

    async def capture(config, args):
        seen["session"] = config.session_file
        return 0

    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
    monkeypatch.setitem(cli._COMMANDS, "groups", capture)
    cli.main(["--session", str(tmp_path / "other.bale"), "groups"])
    assert seen["session"] == tmp_path / "other.bale"


def test_arguments_reach_the_command(monkeypatch):
    seen = {}

    async def capture(config, args):
        seen.update(vars(args))
        return 0

    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
    monkeypatch.setitem(cli._COMMANDS, "history", capture)
    cli.main(["history", "42", "--since", "2026-04-26", "--json"])
    assert seen["chat_id"] == 42 and seen["since"] == "2026-04-26" and seen["json"]


@pytest.fixture(autouse=True)
def _quiet_logging():
    yield
    logging.getLogger().handlers.clear()
