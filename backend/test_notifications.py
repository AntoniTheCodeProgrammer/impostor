import json
import tempfile
import unittest
from pathlib import Path

import libsql_client
from fastapi import HTTPException
from pydantic import ValidationError

import main


class AdminSocket:
    def __init__(self):
        self.events = []

    async def send_text(self, text):
        self.events.append(json.loads(text))


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        main._db = libsql_client.create_client(url=f"file:{Path(self.directory.name) / 'test.db'}")
        await main._db.execute("CREATE TABLE user_pings (username TEXT PRIMARY KEY, pinged_at TEXT)")
        await main._db.execute("INSERT INTO user_pings VALUES ('Ala', 'old')")
        await main.init_db()
        await main._db.batch([
            ("INSERT INTO users (username, password) VALUES (?, ?)", ["Ala", "unused"]),
            ("INSERT INTO users (username, password) VALUES (?, ?)", ["Ola", "unused"]),
        ])
        self.socket = AdminSocket()
        main.active_admin_connections.add(self.socket)
        main._last_ping.clear()
        main._last_message.clear()

    async def asyncTearDown(self):
        main.active_admin_connections.discard(self.socket)
        await main._db.close()
        main._db = None
        self.directory.cleanup()

    async def users(self):
        return {user["username"]: user for user in await main.admin_user_rows(main._db)}

    async def test_message_and_ping_keep_red_until_cleared(self):
        users = await self.users()
        self.assertTrue(users["Ala"]["pinged"])
        self.assertFalse(users["Ala"]["has_message"])

        await main.send_message(main.MessageBody(message="<pomoc>"), "Ala")
        await main.ping("Ala")
        users = await self.users()
        self.assertTrue(users["Ala"]["pinged"])
        self.assertTrue(users["Ala"]["has_message"])
        log = next(event["log"] for event in self.socket.events if event["type"] == "log")
        self.assertEqual((log["username"], log["action"], log["detail"]), ("Ala", "message", "<pomoc>"))
        self.assertTrue(any(event["type"] == "users_upsert" and event["users"][0]["has_message"]
                            for event in self.socket.events))

        await main.clear_ping("Ala")
        self.assertFalse((await self.users())["Ala"]["pinged"])
        await main.ping("Ola")
        self.assertFalse((await self.users())["Ola"]["has_message"])

    async def test_clear_all_notifies_admins_and_keeps_logs(self):
        await main.send_message(main.MessageBody(message="Pomocy"), "Ola")
        self.socket.events.clear()
        await main.clear_all_pings()
        users = await self.users()
        self.assertFalse(users["Ala"]["pinged"])
        self.assertFalse(users["Ola"]["pinged"])
        update = next(event for event in self.socket.events if event["type"] == "users_upsert")
        self.assertEqual({user["username"] for user in update["users"]}, {"Ala", "Ola"})
        self.assertTrue(any(log["detail"] == "Pomocy" for log in (await main.list_logs())["logs"]))

    async def test_message_validation_and_rate_limit(self):
        for value in ("", "x" * 21):
            with self.assertRaises(ValidationError):
                main.MessageBody(message=value)
        with self.assertRaises(HTTPException) as error:
            await main.send_message(main.MessageBody(message="  "), "Ala")
        self.assertEqual(error.exception.status_code, 422)
        await main.send_message(main.MessageBody(message="x" * 20), "Ala")
        with self.assertRaises(HTTPException) as error:
            await main.send_message(main.MessageBody(message="ponownie"), "Ala")
        self.assertEqual(error.exception.status_code, 429)
        with self.assertRaises(HTTPException) as error:
            await main.send_message(main.MessageBody(message="hej"), "nieistniejący")
        self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
