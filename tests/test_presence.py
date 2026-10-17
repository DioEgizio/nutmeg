"""Tests of following Roblox: the client logs followed, the game lookup, the
Discord activity and the Discord IPC client (against a fake Discord), and
closing Roblox when the player leaves a game."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest import mock

from nutmeg import config, discord, presence

from helpers import FakeDiscord, Sandbox, Server

JOIN_LINE = (
    "2026-10-13T10:00:00.000Z,1.0,abc,6 [FLog::Output] ! Joining game "
    "'5d4f1c2e-1111-2222-3333-444455556666' place 1818 at 128.116.1.1\n"
)
LEAVE_LINE = "2026-10-13T10:05:00.000Z,2.0,abc,6 [FLog::SingleSurfaceApp] leaveUGCGameInternal\n"
# What the client writes when the player leaves with the Leave button, and
# when it quits during a game, from a 0.743 client's log.
HOME_LINES = (
    "2026-10-17T00:48:28.800Z,52.800430,d4e87800,6 [FLog::SingleSurfaceApp] returnToLuaApp: (stage:UGCGame).\n"
    "2026-10-17T00:48:28.800Z,52.800480,d4e87800,6 [FLog::SingleSurfaceApp] leaveUGCGameInternal\n"
)
QUIT_LINES = (
    "2026-10-17T00:45:25.870Z,18.870422,d4e87800,6 [FLog::SingleSurfaceApp] shutDown: (stage:UGCGame).\n"
    "2026-10-17T00:45:25.923Z,18.923414,d4e87800,6 [FLog::SingleSurfaceApp] leaveUGCGameInternal\n"
)


class ParseTests(Sandbox):
    def test_lines(self):
        self.assertEqual(presence.parse(JOIN_LINE), ("join", 1818))
        self.assertEqual(presence.parse(LEAVE_LINE), ("leave", None))
        self.assertEqual(presence.parse("x [FLog::Network] Connection lost - Cannot contact server/client"), ("leave", None))
        self.assertIsNone(presence.parse("x [FLog::SingleSurfaceApp] launchUGCGameInternal"))
        self.assertEqual([presence.parse(line) for line in HOME_LINES.splitlines()], [("home", None), ("leave", None)])
        self.assertEqual([presence.parse(line) for line in QUIT_LINES.splitlines()], [None, ("leave", None)])
        # Starting up, the client returns to its home screen from no game.
        self.assertIsNone(presence.parse("x [FLog::SingleSurfaceApp] returnToLuaApp: (stage:InitializedLuaApp)."))


class LogFollowerTests(Sandbox):
    def test_reads_new_lines_of_new_logs(self):
        prefix = self.root / "prefix"
        logs = prefix / "root/Users/me/Library/Logs/Roblox"
        logs.mkdir(parents=True)
        old = logs / "old_last.log"
        old.write_text("stale\n")
        os.utime(old, (1000, 1000))
        self.assertEqual(presence.log_dirs(prefix), [logs, Path.home() / "Library/Logs/Roblox"])
        follower = presence.LogFollower(lambda: presence.log_dirs(prefix), since=2000)
        self.assertEqual(follower.lines(), [])
        new = logs / "0.742_20261013T100000Z_Player_ABC_last.log"
        new.write_text("first\nsecond, half")
        self.assertEqual(follower.lines(), ["first"])
        with open(new, "a") as f:
            f.write(" done\nthird\n")
        self.assertEqual(follower.lines(), ["second, half done", "third"])
        self.assertEqual(follower.lines(), [])
        # In the real home folder, for ROSEMARY_PREFIX_HOME=host.
        host = Path.home() / "Library/Logs/Roblox"
        host.mkdir(parents=True)
        (host / "x.log").write_text("from home\n")
        self.assertEqual(follower.lines(), ["from home"])


class LookupTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.server = Server()
        self.addCleanup(self.server.close)
        for name, path in (
            ("UNIVERSE_URL", "/universe/{place}"),
            ("GAMES_URL", "/games/{universe}"),
            ("ICON_URL", "/icons/{universe}"),
        ):
            patcher = mock.patch.object(presence, name, self.server.url + path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def publish(self) -> None:
        self.server.routes["/universe/1818"] = json.dumps({"universeId": 13058}).encode()
        self.server.routes["/games/13058"] = json.dumps(
            {"data": [{"id": 13058, "name": "Crossroads", "creator": {"name": "Roblox", "type": "User"}}]}
        ).encode()
        self.server.routes["/icons/13058"] = json.dumps(
            {"data": [{"targetId": 13058, "state": "Completed", "imageUrl": "https://tr.rbxcdn.com/icon/512/512/Image/Png"}]}
        ).encode()

    def test_lookup(self):
        self.publish()
        game = presence.lookup(1818)
        self.assertEqual(
            game, presence.Game(1818, "Crossroads", "Roblox", "https://tr.rbxcdn.com/icon/512/512/Image/Png")
        )
        notes = []
        self.assertEqual(presence.lookup(99, notes.append), presence.Game(99))
        self.assertIn("could not look up place 99", notes[0])

    def test_activity(self):
        self.publish()
        result = presence.activity(presence.lookup(1818), 1760000000.5)
        self.assertEqual(
            result,
            {
                "type": 0,
                "details": "Crossroads",
                "state": "by Roblox",
                "timestamps": {"start": 1760000000},
                "assets": {"large_image": "https://tr.rbxcdn.com/icon/512/512/Image/Png", "large_text": "Crossroads"},
                "buttons": [{"label": "See game page", "url": "https://www.roblox.com/games/1818"}],
            },
        )
        bare = presence.activity(presence.Game(5, name="X" * 200), 0)
        self.assertEqual(len(bare["details"]), 128)
        self.assertNotIn("assets", bare)
        self.assertNotIn("state", bare)
        self.assertEqual(presence.activity(presence.Game(5, name="X"), 0)["details"], "X ")

    def test_run_follows_the_game(self):
        self.publish()
        discord_app = FakeDiscord(self.root / "run")
        self.addCleanup(discord_app.close)
        prefix = self.root / "prefix"
        logs = prefix / "root/Users/me/Library/Logs/Roblox"
        logs.mkdir(parents=True)
        running = {"value": {123: "build"}}
        stop = threading.Event()
        log = self.root / "launch.log"
        with mock.patch.object(presence.processes, "running", lambda: running["value"]), \
                mock.patch.object(presence, "POLL_SECONDS", 0.05), \
                mock.patch.object(presence, "GONE_SECONDS", 0.3):
            thread = threading.Thread(target=presence.run, args=(prefix, time.time() - 5, log, stop))
            thread.start()
            path = logs / "player_last.log"
            path.write_text(JOIN_LINE)
            self.assertTrue(self.wait(lambda: len(discord_app.activities) >= 1))
            with open(path, "a") as f:
                f.write(LEAVE_LINE)
            self.assertTrue(self.wait(lambda: len(discord_app.activities) >= 2))
            running["value"] = {}
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(discord_app.client_ids, [presence.CLIENT_ID])
        self.assertEqual(discord_app.activities[0]["details"], "Crossroads")
        self.assertIsNone(discord_app.activities[1])
        text = log.read_text()
        self.assertIn("joined place 1818 (Crossroads)", text)
        self.assertIn("stopped following Roblox", text)

    def test_a_second_copy_gives_way(self):
        with mock.patch.object(presence, "LOCK_WAIT_SECONDS", 0.2):
            held = presence._lock(0)
            try:
                self.assertEqual(presence.run(self.root / "prefix", 0, self.root / "l.log"), 0)
            finally:
                os.close(held)
        self.assertIn("already following", (self.root / "l.log").read_text())

    @staticmethod
    def wait(condition, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False


class CloseOnLeaveTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.discord_app = FakeDiscord(self.root / "run")
        self.addCleanup(self.discord_app.close)
        self.logs = self.root / "prefix/root/Users/me/Library/Logs/Roblox"
        self.logs.mkdir(parents=True)
        self.running = {123: "build"}
        self.terminated: list[list[int]] = []
        self.log = self.root / "launch.log"
        for patch in (
            mock.patch.object(presence.processes, "running", lambda: dict(self.running)),
            mock.patch.object(presence.processes, "terminate", self.terminate),
            mock.patch.object(presence, "lookup", side_effect=AssertionError("looked up with Discord off")),
            mock.patch.object(presence, "POLL_SECONDS", 0.05),
            mock.patch.object(presence, "GONE_SECONDS", 0.3),
            mock.patch.object(presence, "CLOSE_DELAY_SECONDS", 0.1),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def terminate(self, pids: list[int]) -> list[int]:
        self.terminated.append(pids)
        self.running = {}
        return []

    def follow(self, close_on_leave: bool, after_join: str, change_to: bool | None = None) -> str:
        """Run the watcher through joining a game and then `after_join`, with
        the Discord setting off. Returns what it logged."""
        config.save(config.Config(discord_presence=False, close_on_leave=close_on_leave))
        thread = threading.Thread(target=presence.run, args=(self.root / "prefix", time.time() - 5, self.log))
        thread.start()
        path = self.logs / "player_last.log"
        path.write_text(JOIN_LINE)
        self.assertTrue(LookupTests.wait(lambda: self.log.exists() and "joined place 1818" in self.log.read_text()))
        if change_to is not None:
            config.save(config.Config(discord_presence=False, close_on_leave=change_to))
        with open(path, "a") as f:
            f.write(after_join)
        LookupTests.wait(lambda: self.terminated, timeout=0.6)
        self.running = {}
        thread.join(10)
        self.assertFalse(thread.is_alive())
        # The Discord setting is off, so Discord was never contacted.
        self.assertEqual(self.discord_app.client_ids, [])
        return self.log.read_text()

    def test_leaving_closes_roblox(self):
        text = self.follow(True, HOME_LINES)
        self.assertEqual(self.terminated, [[123]])
        self.assertIn("back at the home screen, so Roblox will be closed", text)
        self.assertIn("closed Roblox", text)

    def test_not_when_off(self):
        self.follow(False, HOME_LINES)
        self.assertEqual(self.terminated, [])

    def test_not_on_quitting_or_a_lost_connection(self):
        self.follow(True, QUIT_LINES + "x [FLog::Network] Connection lost - Cannot contact server/client\n")
        self.assertEqual(self.terminated, [])

    def test_joining_again_straight_away_keeps_it(self):
        self.follow(True, HOME_LINES + JOIN_LINE)
        self.assertEqual(self.terminated, [])

    def test_a_change_while_roblox_runs_applies(self):
        self.follow(False, HOME_LINES, change_to=True)
        self.assertEqual(self.terminated, [[123]])


class ClientTests(Sandbox):
    def test_without_discord(self):
        client = discord.Client(presence.CLIENT_ID, timeout=1)
        self.assertFalse(client.connect())
        with self.assertRaises(discord.DiscordError):
            client.set_activity(None)

    def test_with_discord(self):
        discord_app = FakeDiscord(self.root / "run")
        self.addCleanup(discord_app.close)
        self.assertIn(discord_app.path, discord.socket_paths())
        client = discord.Client(presence.CLIENT_ID, timeout=2)
        self.assertTrue(client.connect())
        client.set_activity({"details": "Crossroads"})
        client.set_activity(None)
        client.close()
        self.assertEqual(discord_app.activities, [{"details": "Crossroads"}, None])
