import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from lookout import reolink


def reply(command, value=None, code=0):
    return Reply(json.dumps([{"cmd": command, "code": code, "value": value or {}}]).encode())


class Reply:
    def __init__(self, data=b"", status=200):
        self.data = data
        self.status_code = status
        self.closed = False

    def iter_content(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]

    def close(self):
        self.closed = True


class ReolinkTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session_patch = patch.object(reolink.requests, "Session", return_value=self.session)
        self.factory = self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def replies(self, status=None, logout=None, token="runtime-token"):
        values = [reply("Login", {"Token": {"name": token}}),
                  status if status is not None else reply("GetChannelstatus", {"status": [{"channel": 1, "name": "Front Yard"}]}),
                  logout if logout is not None else reply("Logout")]
        self.session.post.side_effect = values
        return values

    def lookup(self, address="recorder:8000", username="admin", password="private-password"):
        return reolink.camera_names(address, username, password)

    def commands(self):
        return [call.kwargs["params"]["cmd"] for call in self.session.post.call_args_list]

    def test_model_identification_including_generic_manufacturer(self):
        for manufacturer, model in (("Manufacturer", "RLN8-410"), ("", " rln16-410 "),
                                    ("", "RLN36"), ("", "RLN12W"), ("", "RLN8-410-E"),
                                    ("Reolink", "unknown"), ("REOLINK Innovation Limited", "")):
            with self.subTest(manufacturer=manufacturer, model=model):
                self.assertTrue(reolink.is_reolink_model(manufacturer, model))
        for manufacturer, model in (("Manufacturer", "NVR"), ("Other", "RLN8-410 fake"),
                                    ("NotReolink", ""), ("", "RLN999-410"), (None, None)):
            with self.subTest(manufacturer=manufacturer, model=model):
                self.assertFalse(reolink.is_reolink_model(manufacturer, model))

    def test_names_keep_sparse_zero_based_channels_not_source_indices(self):
        expected = {1: "Front Yard", 2: "Front Yard Corner", 3: "Driveway", 4: "Rock Yard", 5: "Pool North"}
        responses = self.replies(reply("GetChannelstatus", {"status": [
            {"channel": 0, "name": "", "online": 0},
            *[{"channel": channel, "name": name, "online": 1} for channel, name in reversed(expected.items())],
        ]}))
        names = self.lookup()
        self.assertEqual(names, expected)
        # Advertised Preview_02 / Preview_06 map to 1 / 5, not VideoS_001 / 005.
        self.assertEqual(names[2 - 1], "Front Yard")
        self.assertEqual(names[6 - 1], "Pool North")
        self.assertEqual(self.commands(), ["Login", "GetChannelstatus", "Logout"])
        self.assertTrue(all(response.closed for response in responses))
        self.session.close.assert_called_once()
        self.assertFalse(self.session.trust_env)

    def test_special_credentials_are_json_only_and_token_is_runtime(self):
        username = "user@name:/?&\"\u00e9"
        password = "s e@c:%/#?&\"\\\u00e9"
        token = "session&token=/?+%"
        self.replies(token=token)
        self.assertEqual(self.lookup(username=username, password=password), {1: "Front Yard"})
        for call in self.session.post.call_args_list:
            kwargs = call.kwargs
            prepared = requests.Request("POST", call.args[0], params=kwargs["params"], json=kwargs["json"]).prepare()
            query = parse_qs(urlsplit(prepared.url).query)
            self.assertEqual(set(query), {"cmd", "token"})
            self.assertNotIn(username, prepared.url)
            self.assertNotIn(password, prepared.url)
            self.assertNotIn("auth", kwargs)
            if query["cmd"] == ["Login"]:
                self.assertEqual(json.loads(prepared.body)[0]["param"]["User"], {"userName": username, "password": password})
                self.assertEqual(query["token"], ["null"])
            else:
                self.assertEqual(query["token"], [token])
                self.assertNotIn(password.encode(), prepared.body)
            self.assertTrue(kwargs["verify"])
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["stream"])
            self.assertTrue(all(0 < value <= 3 for value in kwargs["timeout"]))

    def test_same_host_endpoint_and_https_preservation(self):
        cases = {
            "recorder": "http://recorder/cgi-bin/api.cgi",
            "recorder:8000": "http://recorder/cgi-bin/api.cgi",
            "http://recorder:8000/onvif/device_service": "http://recorder/cgi-bin/api.cgi",
            "HTTPS://Recorder:8000/onvif/device_service": "https://recorder/cgi-bin/api.cgi",
            "https://recorder": "https://recorder/cgi-bin/api.cgi",
            "https://recorder:8443/custom": "https://recorder:8443/cgi-bin/api.cgi",
            "http://recorder:8080": "http://recorder:8080/cgi-bin/api.cgi",
            "2001:db8::1": "http://[2001:db8::1]/cgi-bin/api.cgi",
            "https://[2001:db8::1]:8000/onvif": "https://[2001:db8::1]/cgi-bin/api.cgi",
            "http://recorder//other-host/cgi-bin/api.cgi": "http://recorder/cgi-bin/api.cgi",
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.session.reset_mock()
                self.replies()
                self.assertEqual(self.lookup(address), {1: "Front Yard"})
                self.assertEqual([call.args[0] for call in self.session.post.call_args_list], [expected] * 3)

    def test_invalid_addresses_never_open_network(self):
        for address in (None, "", "rtsp://recorder/live", "http://user:secret@recorder", "http://recorder?password=secret",
                        "http://recorder/#secret", "http://recorder:0", "http://recorder:65536", "http://recorder:bad",
                        "http://recorder\\other-host", "http://%72ecorder", "http://bad host", "http://recorder\n.other",
                        "http://[broken", "http://recorder/\ud800", "http://" + "x" * 2048):
            with self.subTest(address=address):
                self.assertEqual(self.lookup(address), {})
        self.factory.assert_not_called()

    def test_http_auth_and_redirect_failures_do_not_retry_or_follow(self):
        for status in (301, 302, 307, 308, 401, 403, 404, 500):
            with self.subTest(status=status):
                self.session.reset_mock()
                response = Reply(status=status)
                response.headers = {"Location": "http://other-host/cgi-bin/api.cgi"}
                self.session.post.side_effect = [response]
                self.assertEqual(self.lookup("https://recorder:8000"), {})
                self.assertEqual(self.commands(), ["Login"])
                self.assertFalse(self.session.post.call_args.kwargs["allow_redirects"])
                self.assertTrue(response.closed)
                self.session.close.assert_called_once()

    def test_api_login_denied_and_invalid_tokens(self):
        for response in (reply("Login", code=1), reply("Login"), reply("Login", {"Token": []}),
                         *[reply("Login", {"Token": {"name": token}}) for token in (None, "", 123, "x" * 4097)]):
            with self.subTest(response=response):
                self.session.reset_mock()
                self.session.post.side_effect = [response]
                self.assertEqual(self.lookup(), {})
                self.assertEqual(self.commands(), ["Login"])
                self.assertTrue(response.closed)
                self.session.close.assert_called_once()

    def test_network_tls_and_timeout_failures_are_silent_no_downgrade(self):
        for error in (requests.Timeout("private-password"), requests.exceptions.SSLError("private-password"),
                      requests.ConnectionError("private-password"), OSError("private-password")):
            for stage in ("login", "status"):
                with self.subTest(error=type(error).__name__, stage=stage):
                    self.session.reset_mock()
                    if stage == "login":
                        self.session.post.side_effect = [error]
                    else:
                        self.replies(status=error)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        self.assertEqual(self.lookup("https://recorder:8000"), {})
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(self.commands(), ["Login"] if stage == "login" else ["Login", "GetChannelstatus", "Logout"])
                    for call in self.session.post.call_args_list:
                        self.assertEqual(call.args[0], "https://recorder/cgi-bin/api.cgi")
                        self.assertTrue(call.kwargs["verify"])
                    self.session.close.assert_called_once()

    def test_denied_or_malformed_status_still_logs_out(self):
        responses = [reply("GetChannelstatus", code=1), Reply(status=403), Reply(b"not json"), Reply(b"\xff"),
                     Reply(b"[" * 1100 + b"]" * 1100), Reply(b"{}"), Reply(b"[]"), Reply(b"[null]"),
                     reply("OtherCommand"), reply("GetChannelstatus", code=False), reply("GetChannelstatus"),
                     reply("GetChannelstatus", {"status": {}}), reply("GetChannelstatus", {"status": [None] * 257})]
        for response in responses:
            with self.subTest(response=response):
                self.session.reset_mock()
                values = self.replies(status=response)
                self.assertEqual(self.lookup(), {})
                self.assertEqual(self.commands(), ["Login", "GetChannelstatus", "Logout"])
                self.assertTrue(all(value.closed for value in values))
                self.session.close.assert_called_once()

    def test_response_size_is_bounded_and_session_logs_out(self):
        response = Reply(b" " * (reolink.MAX_JSON + 1))
        self.replies(status=response)
        self.assertEqual(self.lookup(), {})
        self.assertTrue(response.closed)
        self.assertEqual(self.commands(), ["Login", "GetChannelstatus", "Logout"])

    def test_response_deadline_and_separate_logout_budget(self):
        response = Reply(b" ")

        def slow_chunks(size):
            clock.return_value = reolink.LOOKUP_TIMEOUT + 1
            yield b" "

        response.iter_content = slow_chunks
        with patch.object(reolink.time, "monotonic", return_value=0) as clock:
            self.replies(status=response)
            self.assertEqual(self.lookup(), {})
        self.assertTrue(response.closed)
        self.assertEqual(self.commands(), ["Login", "GetChannelstatus", "Logout"])
        self.assertEqual(self.session.post.call_args.kwargs["timeout"], (1.5, 1.5))
        self.session.close.assert_called_once()

    def test_logout_failure_does_not_discard_names_or_leak_session(self):
        for logout in (requests.Timeout("runtime-token"), reply("Logout", code=1), Reply(b"bad response")):
            with self.subTest(logout=logout):
                self.session.reset_mock()
                self.replies(logout=logout)
                self.assertEqual(self.lookup(), {1: "Front Yard"})
                self.assertEqual(self.commands(), ["Login", "GetChannelstatus", "Logout"])
                self.session.close.assert_called_once()

    def test_expired_budget_prevents_request_but_still_allows_logout(self):
        for times, expected in (([0, 9], []), ([0, 0, 0, 9, 9, 9, 9], ["Login", "Logout"])):
            with self.subTest(expected=expected):
                self.session.reset_mock()
                self.session.post.side_effect = [reply("Login", {"Token": {"name": "runtime-token"}}), reply("Logout")]
                with patch.object(reolink.time, "monotonic", side_effect=times):
                    self.assertEqual(self.lookup(), {})
                self.assertEqual(self.commands(), expected)
                self.session.close.assert_called_once()

    def test_close_failure_is_optional_and_follows_logout(self):
        self.replies()
        self.session.close.side_effect = OSError("private-password")
        self.assertEqual(self.lookup(), {1: "Front Yard"})
        self.assertEqual(self.commands(), ["Login", "GetChannelstatus", "Logout"])
        self.session.close.assert_called_once()

    def test_only_valid_names_and_indices_are_returned(self):
        self.replies(reply("GetChannelstatus", {"status": [
            None, [], {}, {"channel": "0", "name": "Wrong"}, {"channel": True, "name": "Wrong"},
            {"channel": -1, "name": "Wrong"}, {"channel": 256, "name": "Wrong"},
            {"channel": 0, "name": "  Front  "}, {"channel": 0, "name": "Duplicate"},
            {"channel": 2, "name": "  "}, {"channel": 3, "name": None}, {"channel": 4, "name": "x" * 257},
            {"channel": 5, "name": "bad\x00name"}, {"channel": 6, "name": "bad\ud800name"},
            {"channel": 7, "name": "bad\nname"}, {"channel": 8, "name": "bad\x7fname"},
            {"channel": 10, "name": "Entr\u00e9e", "online": 0},
        ]}))
        self.assertEqual(self.lookup(), {0: "Front", 10: "Entr\u00e9e"})


if __name__ == "__main__":
    unittest.main()
