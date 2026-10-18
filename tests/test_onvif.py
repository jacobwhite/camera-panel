import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import socket
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from lookout import onvif
from lookout.onvif import Camera, ONVIFClient, OnvifError, discover, normalize_address, stream_uri


def response(body):
    return f'<s:Envelope xmlns:s="{onvif.SOAP}" xmlns:t="urn:test"><s:Body>{body}</s:Body></s:Envelope>'.encode()


def profile(token, source="channel-1", width=640, height=360, media2=False, name="Front", config=""):
    source_xml = f'<t:SourceToken>{source}</t:SourceToken>' if source else ""
    if media2:
        configuration = (
            f'<t:Configurations><t:VideoSource token="{config}"><t:Name>{name}</t:Name>{source_xml}</t:VideoSource>'
            f'<t:VideoEncoder><t:Resolution><t:Width>{width}</t:Width><t:Height>{height}</t:Height>'
            '</t:Resolution></t:VideoEncoder></t:Configurations>'
        )
    else:
        configuration = (
            f'<t:VideoSourceConfiguration token="{config}"><t:Name>{name}</t:Name>{source_xml}</t:VideoSourceConfiguration>'
            f'<t:VideoEncoderConfiguration><t:Resolution><t:Width>{width}</t:Width><t:Height>{height}</t:Height>'
            '</t:Resolution></t:VideoEncoderConfiguration>'
        )
    return f'<t:Profiles token="{token}"><t:Name>{name} profile</t:Name>{configuration}</t:Profiles>'


CLOCK = response(
    '<t:GetSystemDateAndTimeResponse><t:SystemDateAndTime><t:UTCDateTime>'
    '<t:Time><t:Hour>3</t:Hour><t:Minute>4</t:Minute><t:Second>5</t:Second></t:Time>'
    '<t:Date><t:Year>2020</t:Year><t:Month>1</t:Month><t:Day>2</t:Day></t:Date>'
    '</t:UTCDateTime></t:SystemDateAndTime></t:GetSystemDateAndTimeResponse>'
)
CAPABILITIES = response('<t:GetCapabilitiesResponse><t:Capabilities><t:Media><t:XAddr>http://0.0.0.0/onvif/media</t:XAddr></t:Media></t:Capabilities></t:GetCapabilitiesResponse>')
SERVICES = response(f'<t:GetServicesResponse><t:Service><t:Namespace>{onvif.MEDIA2}</t:Namespace><t:XAddr>http://recorder/onvif/media2</t:XAddr></t:Service></t:GetServicesResponse>')
UNSUPPORTED = response('<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>t:ActionNotSupported</s:Value></s:Subcode></s:Code></s:Fault>')


class Reply:
    def __init__(self, data, status=200, headers=None):
        self.data = data
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]

    def close(self):
        self.closed = True


class URLTests(unittest.TestCase):
    def test_normalize_address(self):
        cases = {
            "192.168.1.10": "http://192.168.1.10/onvif/device_service",
            " recorder:8000 ": "http://recorder:8000/onvif/device_service",
            "HTTPS://Recorder:443/": "https://recorder/onvif/device_service",
            "http://recorder:80/custom/device": "http://recorder/custom/device",
            "2001:db8::1": "http://[2001:db8::1]/onvif/device_service",
            "[2001:db8::1]:8080": "http://[2001:db8::1]:8080/onvif/device_service",
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(normalize_address(address), expected)

    def test_invalid_addresses_are_safe(self):
        for address in ("", "rtsp://recorder/a", "http://u:secret@recorder", "http://r?password=secret", "http://r/#secret", "http://r:65536", "http://r:bad", "http://bad host", "http://r\\evil", "http://%72ecorder"):
            with self.subTest(address=address), self.assertRaises(OnvifError) as caught:
                normalize_address(address)
            self.assertNotIn("secret", str(caught.exception))

    def test_credentials_are_encoded_once(self):
        camera = Camera("id", "Front", "rtsp://recorder:554/live?channel=1", "nvr", "Home", "a@b:/", "s e@c:%/#?")
        uri = stream_uri(camera)
        self.assertEqual(uri, "rtsp://a%40b%3A%2F:s%20e%40c%3A%25%2F%23%3F@recorder:554/live?channel=1")
        camera.uri = uri
        self.assertEqual(stream_uri(camera), uri)
        self.assertNotIn(camera.password, repr(camera))
        self.assertNotIn(uri, repr(camera))

    def test_advertised_credentials_are_not_overwritten(self):
        camera = Camera("id", "Front", "rtsp://device:own%40secret@recorder/live", "nvr", "Home", "admin", "secret")
        self.assertEqual(stream_uri(camera), camera.uri)
        camera.uri = "rtsp://admin@recorder/live"
        self.assertEqual(stream_uri(camera), "rtsp://admin:secret@recorder/live")
        camera.uri = "rtsp://different@recorder/live"
        self.assertEqual(stream_uri(camera), camera.uri)
        camera.uri = "rtsp://admin:@recorder/live"
        self.assertEqual(stream_uri(camera), camera.uri)

    def test_empty_credentials_and_ipv6(self):
        camera = Camera("id", "Front", "rtsps://[2001:db8::1]:554/live", "nvr", "Home")
        self.assertEqual(stream_uri(camera), camera.uri)
        camera.username, camera.password = "u", "p"
        self.assertEqual(stream_uri(camera), "rtsps://u:p@[2001:db8::1]:554/live")

    def test_localhost_and_wildcard_repair(self):
        for host in ("0.0.0.0", "localhost", "127.0.0.4", "[::]", "[::1]"):
            self.assertEqual(onvif._repair_host(f"rtsp://u:p@{host}:8554/live", "192.168.1.3"), "rtsp://u:p@192.168.1.3:8554/live")
        self.assertEqual(onvif._repair_host("rtsp://192.168.1.4/live", "192.168.1.3"), "rtsp://192.168.1.4/live")
        self.assertEqual(onvif._repair_host("rtsp://0.0.0.0/live", "2001:db8::2"), "rtsp://[2001:db8::2]/live")


class DiscoveryTests(unittest.TestCase):
    XML = response(
        '<t:ProbeMatches><t:ProbeMatch><t:Scopes>'
        'onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/hardware/Model%20X'
        '</t:Scopes><t:XAddrs>http://0.0.0.0/onvif/device_service http://192.168.1.8:80/onvif/device_service rtsp://invalid/</t:XAddrs>'
        '</t:ProbeMatch></t:ProbeMatches>'
    )

    def test_friendly_name_hardware_and_invalid_xml(self):
        devices = onvif._discovery_devices(self.XML, "192.168.1.8")
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0], onvif.DiscoveredDevice("Front Door", "http://192.168.1.8/onvif/device_service", "Model X"))
        for data in (b"not xml", b"<broken>", b'<!DOCTYPE x [<!ENTITY e "secret">]><x>&e;</x>', b"<x>\x00</x>"):
            self.assertEqual(onvif._discovery_devices(data, "192.168.1.8"), [])

    @patch("lookout.onvif.subprocess.run")
    def test_interface_enumeration_is_not_a_scan(self, run):
        run.return_value.stdout = '''[
          {"flags":["UP","LOOPBACK"],"addr_info":[{"family":"inet","local":"127.0.0.1"}]},
          {"flags":["UP"],"addr_info":[{"family":"inet","local":"192.168.1.2"},{"family":"inet6","local":"::1"}]},
          {"flags":["UP"],"addr_info":[{"family":"inet","local":"10.0.0.2"},{"family":"inet","local":"192.168.1.2"}]}
        ]'''
        self.assertEqual(onvif._ipv4_interfaces(0.5), ["192.168.1.2", "10.0.0.2"])
        self.assertEqual(run.call_args.args[0], ["ip", "-j", "-4", "addr", "show", "up"])
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)
        run.side_effect = subprocess.TimeoutExpired("ip", 0.5)
        self.assertEqual(onvif._ipv4_interfaces(0.5), ["0.0.0.0"])

    def test_multicast_each_interface_dedup_and_close(self):
        first, second = Mock(), Mock()
        first.recvfrom.return_value = (self.XML, ("192.168.1.8", 3702))
        second.recvfrom.return_value = (self.XML, ("192.168.1.8", 3702))
        with patch.object(onvif, "_ipv4_interfaces", return_value=["192.168.1.2", "10.0.0.2"]), \
                patch.object(onvif.socket, "socket", side_effect=[first, second]), \
                patch.object(onvif.select, "select", side_effect=[([first, second], [], []), ([], [], [])]) as select_mock:
            devices = discover(0.1)
        self.assertEqual(len(devices), 1)
        for sock, interface in ((first, "192.168.1.2"), (second, "10.0.0.2")):
            sock.bind.assert_called_once_with((interface, onvif.DISCOVERY_PORT))
            sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
            self.assertEqual(sock.sendto.call_args.args[1], ("239.255.255.250", 3702))
            ET.fromstring(sock.sendto.call_args.args[0])
            sock.close.assert_called_once()
        self.assertLessEqual(select_mock.call_args.args[3], 0.1)

    def test_zero_timeout_does_not_open_network(self):
        with patch.object(onvif, "_ipv4_interfaces") as interfaces:
            for timeout in (0, -1, float("nan"), float("inf")):
                self.assertEqual(discover(timeout), [])
        interfaces.assert_not_called()

    def test_failed_interface_is_closed(self):
        sock = Mock()
        sock.sendto.side_effect = OSError("not available")
        with patch.object(onvif, "_ipv4_interfaces", return_value=["10.0.0.2"]), patch.object(onvif.socket, "socket", return_value=sock):
            self.assertEqual(discover(0.1), [])
        sock.close.assert_called_once()

    def test_discovery_device_count_is_bounded(self):
        sock = Mock()
        sock.recvfrom.return_value = (self.XML, ("192.168.1.8", 3702))
        devices = [onvif.DiscoveredDevice(str(i), f"http://camera-{i}/onvif/device_service") for i in range(300)]
        with patch.object(onvif, "_ipv4_interfaces", return_value=["10.0.0.2"]), \
                patch.object(onvif.socket, "socket", return_value=sock), \
                patch.object(onvif.select, "select", return_value=([sock], [], [])), \
                patch.object(onvif, "_discovery_devices", return_value=devices):
            self.assertEqual(len(discover(0.1)), onvif.MAX_DISCOVERED)
        sock.recvfrom.assert_called_once()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session_patch = patch.object(onvif.requests, "Session", return_value=self.session)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def replies(self, *values):
        replies = [value if isinstance(value, (Reply, Exception)) else Reply(value) for value in values]
        self.session.post.side_effect = replies
        return replies

    def media1(self, profiles, *streams, username="admin", password="private-password"):
        self.replies(CLOCK, CAPABILITIES, response(f"<t:GetProfilesResponse>{profiles}</t:GetProfilesResponse>"), *[
            response(f"<t:GetStreamUriResponse><t:MediaUri><t:Uri>{uri}</t:Uri></t:MediaUri></t:GetStreamUriResponse>") for uri in streams
        ])
        return ONVIFClient("recorder", username, password).cameras("nvr-1", "Home")

    def test_substreams_grouped_by_source_not_resolution(self):
        cameras = self.media1(
            profile("main", width=1920, height=1080) + profile("sub&amp;1") + profile("second", source="channel-2", name="Back"),
            "rtsp://0.0.0.0:554/live?channel=1&amp;sub=1", "rtsp://localhost/live?channel=2",
        )
        self.assertEqual(len(cameras), 2)
        self.assertEqual(cameras[0].name, "Front")
        self.assertEqual((cameras[0].width, cameras[0].height), (640, 360))
        self.assertEqual(cameras[0].uri, "rtsp://recorder:554/live?channel=1&sub=1")
        self.assertEqual(cameras[0].nvr_id, "nvr-1")
        self.assertNotEqual(cameras[0].id, cameras[1].id)
        stream_calls = self.session.post.call_args_list[3:]
        self.assertEqual(onvif._text(ET.fromstring(stream_calls[0].kwargs["data"]), "ProfileToken"), "sub&1")
        self.assertEqual(stream_calls[0].args[0], "http://recorder/onvif/media")
        self.session.close.assert_called_once()

    def test_lowest_valid_resolution_is_preferred_by_area(self):
        for media2 in (False, True):
            with self.subTest(media2=media2):
                profiles = (
                    profile("invalid", width=0, height=0, media2=media2)
                    + profile("main", width=1920, height=1080, media2=media2)
                    + profile("narrow", width=320, height=1080, media2=media2)
                    + profile("sub", width=640, height=360, media2=media2)
                )
                self.replies(CLOCK, CAPABILITIES.replace(b"Media", b"Media2") if media2 else CAPABILITIES,
                             response(f'<t:GetProfilesResponse>{profiles}</t:GetProfilesResponse>'),
                             response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/sub</t:Uri></t:GetStreamUriResponse>'))
                delivered = []
                cameras = ONVIFClient("recorder").cameras("id", "Home", on_camera=delivered.append)
                self.assertEqual(len(cameras), 1)
                self.assertEqual((cameras[0].width, cameras[0].height), (640, 360))
                self.assertEqual(onvif._text(ET.fromstring(self.session.post.call_args.kwargs["data"]), "ProfileToken"), "sub")
                self.assertEqual(delivered, cameras)
                self.assertIsNot(delivered[0], cameras[0])

    def test_callback_copies_arrive_before_later_uris_and_friendly_names(self):
        for media2 in (False, True):
            with self.subTest(media2=media2):
                replies = iter(self.replies(
                    CLOCK, CAPABILITIES.replace(b"Media", b"Media2") if media2 else CAPABILITIES,
                    response(f'<t:GetProfilesResponse>{profile("011", name="VideoS_001", media2=media2)}'
                             f'{profile("021", source="channel-2", name="VideoS_002", media2=media2)}</t:GetProfilesResponse>'),
                    response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/h264Preview_01_sub</t:Uri></t:GetStreamUriResponse>'),
                    response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/h264Preview_02_sub</t:Uri></t:GetStreamUriResponse>'),
                    response('<t:GetDeviceInformationResponse><t:Model>RLN8-410</t:Model></t:GetDeviceInformationResponse>'),
                ))
                events, delivered = [], []

                def post(*args, **kwargs):
                    events.append(kwargs["headers"]["SOAPAction"].strip('"').rsplit("/", 1)[-1])
                    return next(replies)

                def on_camera(camera):
                    delivered.append(camera)
                    events.append(camera.name)

                def names(*args):
                    events.append("names")
                    return {0: "Front Yard", 1: "Pool North"}

                self.session.post.side_effect = post
                with patch.object(onvif, "camera_names", side_effect=names):
                    cameras = ONVIFClient("recorder", "admin", "secret").cameras("id", "Home", on_camera=on_camera)
                self.assertEqual(events, ["GetSystemDateAndTime", "GetCapabilities", "GetProfiles",
                                          "GetStreamUri", "VideoS_001", "GetStreamUri", "VideoS_002",
                                          "GetDeviceInformation", "names"])
                self.assertEqual([camera.name for camera in delivered], ["VideoS_001", "VideoS_002"])
                self.assertEqual([camera.name for camera in cameras], ["Front Yard", "Pool North"])
                for early, final in zip(delivered, cameras):
                    self.assertIsNot(early, final)
                    self.assertEqual(early.id, final.id)
                    self.assertEqual(early.uri, final.uri)
                    self.assertEqual((early.username, early.password), ("admin", "secret"))

    def test_callback_mutations_and_errors_do_not_break_lookup(self):
        for error in (RuntimeError, OnvifError, onvif._UnsupportedAction):
            with self.subTest(error=error):
                self.session.reset_mock()
                self.replies(CLOCK, CAPABILITIES,
                             response(f'<t:GetProfilesResponse>{profile("main", width=1920)}{profile("sub")}'
                                      f'{profile("second", source="channel-2", name="Back")}</t:GetProfilesResponse>'),
                             response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/sub</t:Uri></t:GetStreamUriResponse>'),
                             response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/back</t:Uri></t:GetStreamUriResponse>'))
                delivered_ids = []

                def on_camera(camera):
                    delivered_ids.append(camera.id)
                    camera.id, camera.name, camera.uri = "changed", "changed", "changed"
                    raise error("callback failed")

                cameras = ONVIFClient("recorder").cameras("id", "Home", on_camera=on_camera)
                self.assertEqual([camera.id for camera in cameras], delivered_ids)
                self.assertEqual([camera.name for camera in cameras], ["Front", "Back"])
                self.assertEqual([camera.uri for camera in cameras], ["rtsp://recorder/sub", "rtsp://recorder/back"])
                self.assertEqual(self.session.post.call_count, 5)
                self.session.close.assert_called_once()

    def test_parallel_clients_keep_callbacks_and_sessions_on_their_workers(self):
        sessions = [Mock(), Mock()]
        for session in sessions:
            session.post.side_effect = [Reply(body) for body in (
                CLOCK, CAPABILITIES,
                response(f'<t:GetProfilesResponse>{profile("sub")}</t:GetProfilesResponse>'),
                response('<t:GetStreamUriResponse><t:Uri>rtsp://0.0.0.0/sub</t:Uri></t:GetStreamUriResponse>'),
            )]
        barrier = threading.Barrier(2, timeout=5)

        def lookup(host):
            delivered = []

            def on_camera(camera):
                barrier.wait()
                delivered.append((camera, threading.get_ident()))

            client = ONVIFClient(host)
            cameras = client.cameras(host, "Home", on_camera=on_camera)
            return cameras, delivered, threading.get_ident(), client._session

        with patch.object(onvif.requests, "Session", side_effect=sessions), ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lookup, ("first", "second")))
        self.assertNotEqual(results[0][2], results[1][2])
        self.assertIsNot(results[0][3], results[1][3])
        for cameras, delivered, thread_id, session in results:
            self.assertEqual(delivered, [(cameras[0], thread_id)])
            self.assertNotEqual(thread_id, threading.get_ident())
            self.assertTrue(all(urlsplit(call.args[0]).hostname == cameras[0].nvr_id for call in session.post.call_args_list))
            self.assertFalse(session.trust_env)
            session.close.assert_called_once()

    def test_callback_is_keyword_only(self):
        with self.assertRaises(TypeError):
            ONVIFClient("recorder").cameras("id", "Home", Mock())
        self.session.post.assert_not_called()

    def test_channels_without_source_identity_are_not_collapsed(self):
        cameras = self.media1(profile("one", source="") + profile("two", source=""), "rtsp://recorder/1", "rtsp://recorder/2")
        self.assertEqual(len(cameras), 2)
        self.assertNotEqual(cameras[0].id, cameras[1].id)

    def test_shared_source_configuration_can_be_grouped(self):
        cameras = self.media1(profile("one", source="", config="source-config", width=1920) + profile("two", source="", config="source-config"), "rtsp://recorder/2")
        self.assertEqual(len(cameras), 1)
        self.assertEqual(onvif._text(ET.fromstring(self.session.post.call_args.kwargs["data"]), "ProfileToken"), "two")

    def test_cross_host_stream_does_not_get_recorder_credentials(self):
        cameras = self.media1(profile("one") + profile("two", source="channel-2"), "rtsp://192.168.1.50/live", "rtsp://other:own%40password@192.168.1.51/live")
        for camera in cameras:
            self.assertEqual((camera.username, camera.password), ("", ""))
            self.assertEqual(stream_uri(camera), camera.uri)

    def test_capabilities_preserve_canonical_device_address(self):
        capabilities = CAPABILITIES.replace(b"<t:Capabilities>", b"<t:Capabilities><t:Device>"
                                            b"<t:XAddr>http://recorder:8000/onvif/device_service</t:XAddr></t:Device>")
        self.replies(CLOCK, capabilities,
                     response(f'<t:GetProfilesResponse>{profile("one")}</t:GetProfilesResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
        client = ONVIFClient("recorder")
        self.assertEqual(len(client.cameras("id", "Home")), 1)
        self.assertEqual(client.device_addresses, {"http://recorder/onvif/device_service",
                                                   "http://recorder:8000/onvif/device_service"})
        self.assertEqual(onvif._text(ET.fromstring(self.session.post.call_args_list[1].kwargs["data"]), "Category"), "All")

    def test_untrusted_device_alias_does_not_hide_another_recorder(self):
        for address in ("http://other-recorder:8000/onvif/device_service", "http://user:secret@recorder/device", "invalid://recorder"):
            with self.subTest(address=address):
                capabilities = CAPABILITIES.replace(b"<t:Capabilities>",
                    f"<t:Capabilities><t:Device><t:XAddr>{address}</t:XAddr></t:Device>".encode())
                self.replies(CLOCK, capabilities,
                             response(f'<t:GetProfilesResponse>{profile("one")}</t:GetProfilesResponse>'),
                             response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
                client = ONVIFClient("recorder")
                self.assertEqual(len(client.cameras("id", "Home")), 1)
                self.assertEqual(client.device_addresses, {"http://recorder/onvif/device_service"})

    def test_getservices_can_identify_device_address(self):
        services = SERVICES.replace(b"<t:GetServicesResponse>",
            f'<t:GetServicesResponse><t:Service><t:Namespace>{onvif.DEVICE}</t:Namespace>'
            '<t:XAddr>http://recorder:8000/onvif/device_service</t:XAddr></t:Service>'.encode())
        self.replies(CLOCK, Reply(UNSUPPORTED, 500), services,
                     response(f'<t:GetProfilesResponse>{profile("m2", media2=True)}</t:GetProfilesResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
        client = ONVIFClient("recorder")
        self.assertEqual(len(client.cameras("id", "Home")), 1)
        self.assertIn("http://recorder:8000/onvif/device_service", client.device_addresses)

    def test_ws_security_clock_compensation_and_transport(self):
        password = "p<&:secret"
        self.media1(profile("sub"), "rtsp://recorder/live", username="u<&", password=password)
        self.assertFalse(self.session.trust_env)
        first_call = self.session.post.call_args_list[0]
        self.assertIsNone(first_call.kwargs["auth"])
        self.assertNotIn(b"UsernameToken", first_call.kwargs["data"])
        for call in self.session.post.call_args_list[1:]:
            self.assertIsInstance(call.kwargs["auth"], HTTPDigestAuth)
            self.assertTrue(call.kwargs["verify"])
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])
            self.assertLessEqual(call.kwargs["timeout"][0], 2)
            self.assertLessEqual(call.kwargs["timeout"][1], 8)
            root = ET.fromstring(call.kwargs["data"])
            self.assertEqual(onvif._text(root, "Username"), "u<&")
            created = onvif._text(root, "Created")
            date = datetime.fromisoformat(created)
            self.assertLess(abs((date - datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)).total_seconds()), 5)
            digest = hashlib.sha1(base64.b64decode(onvif._text(root, "Nonce")) + created.encode() + password.encode()).digest()
            self.assertEqual(onvif._text(root, "Password"), base64.b64encode(digest).decode())
            self.assertNotIn(password.encode(), call.kwargs["data"])

    def test_basic_fallback_only_on_explicit_challenge(self):
        self.replies(CLOCK, Reply(b"denied", 401, {"WWW-Authenticate": 'Basic realm="camera"'}), CAPABILITIES,
                     response(f'<t:GetProfilesResponse>{profile("one")}</t:GetProfilesResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
        self.assertEqual(len(ONVIFClient("recorder", "admin", "secret").cameras("id", "Home")), 1)
        self.assertIsInstance(self.session.post.call_args_list[1].kwargs["auth"], HTTPDigestAuth)
        self.assertIsInstance(self.session.post.call_args_list[2].kwargs["auth"], HTTPBasicAuth)

    def test_digest_realm_named_basic_does_not_enable_basic_auth(self):
        self.replies(CLOCK, Reply(b"denied", 401, {"WWW-Authenticate": 'Digest realm="Basic", nonce="abc"'}))
        with self.assertRaisesRegex(OnvifError, "authentication"):
            ONVIFClient("recorder", "admin", "secret").cameras("id", "Home")
        self.assertEqual(self.session.post.call_count, 2)

    def test_media2_fallback_for_missing_or_unsupported_media1(self):
        for capabilities, extra in ((Reply(UNSUPPORTED, 500), []), (CAPABILITIES, [Reply(UNSUPPORTED, 500)])):
            with self.subTest(capabilities=capabilities):
                self.session.reset_mock()
                self.replies(CLOCK, capabilities, *extra, SERVICES,
                             response(f'<t:GetProfilesResponse>{profile("m2", media2=True)}</t:GetProfilesResponse>'),
                             response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
                cameras = ONVIFClient("recorder").cameras("id", "Home")
                self.assertEqual(len(cameras), 1)
                call = self.session.post.call_args
                self.assertEqual(call.args[0], "http://recorder/onvif/media2")
                self.assertIn(onvif.MEDIA2, call.kwargs["headers"]["SOAPAction"])
                self.assertEqual(onvif._text(ET.fromstring(call.kwargs["data"]), "Protocol"), "RTSP")

    def test_failing_substream_tries_next_profile(self):
        self.replies(CLOCK, CAPABILITIES,
                     response(f'<t:GetProfilesResponse>{profile("sub")}{profile("main", width=1920, height=1080)}</t:GetProfilesResponse>'),
                     response('<s:Fault><s:Reason><s:Text>private-password</s:Text></s:Reason></s:Fault>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/main</t:Uri></t:GetStreamUriResponse>'))
        cameras = ONVIFClient("recorder").cameras("id", "Home")
        self.assertEqual(cameras[0].width, 1920)

    def test_media2_capabilities_do_not_require_getservices(self):
        self.replies(CLOCK, CAPABILITIES.replace(b"Media", b"Media2"),
                     response(f'<t:GetProfilesResponse>{profile("m2", media2=True)}</t:GetProfilesResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
        self.assertEqual(len(ONVIFClient("recorder").cameras("id", "Home")), 1)
        self.assertEqual(self.session.post.call_count, 4)
        self.assertTrue(all("GetServices" not in call.kwargs["headers"]["SOAPAction"] for call in self.session.post.call_args_list))

    def test_media2_fallback_when_media1_getstreamuri_is_unsupported(self):
        self.replies(CLOCK, CAPABILITIES,
                     response(f'<t:GetProfilesResponse>{profile("m1")}</t:GetProfilesResponse>'), Reply(UNSUPPORTED, 500), SERVICES,
                     response(f'<t:GetProfilesResponse>{profile("m2", media2=True)}</t:GetProfilesResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
        cameras = ONVIFClient("recorder").cameras("id", "Home")
        self.assertEqual(len(cameras), 1)
        self.assertIn(onvif.MEDIA2, self.session.post.call_args.kwargs["headers"]["SOAPAction"])

    def test_clock_lookup_failure_does_not_prevent_stream_lookup(self):
        self.replies(Reply(b"denied", 401), Reply(UNSUPPORTED, 500), CAPABILITIES,
                     response(f'<t:GetProfilesResponse>{profile("one")}</t:GetProfilesResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/live</t:Uri></t:GetStreamUriResponse>'))
        self.assertEqual(len(ONVIFClient("recorder", "admin", "secret").cameras("id", "Home")), 1)
        self.assertIsNone(self.session.post.call_args_list[0].kwargs["auth"])
        self.assertIsInstance(self.session.post.call_args_list[1].kwargs["auth"], HTTPDigestAuth)

    def test_missing_resolution_and_audio_only_profiles(self):
        cameras = self.media1(
            '<t:Profiles token="audio"><t:AudioEncoderConfiguration/></t:Profiles>' + profile("video", width="bad", height=0),
            "rtsp://recorder/video",
        )
        self.assertEqual(len(cameras), 1)
        self.assertEqual((cameras[0].width, cameras[0].height), (640, 360))

    def test_ids_are_stable_across_substream_profile_changes(self):
        first = self.media1(profile("old", width=1920), "rtsp://recorder/old")
        second = self.media1(profile("new"), "rtsp://recorder/new")
        self.assertEqual(first[0].id, second[0].id)

    @patch("lookout.onvif.camera_names", return_value={1: "Front Yard", 5: "Pool North"})
    def test_reolink_names_use_advertised_channel_not_source_token(self, names):
        self.replies(CLOCK, CAPABILITIES,
                     response(f'<t:GetProfilesResponse>{profile("011", source="001", name="VideoS_001")}'
                              f'{profile("051", source="005", name="VideoS_005")}</t:GetProfilesResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/h264Preview_02_sub</t:Uri></t:GetStreamUriResponse>'),
                     response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/h264Preview_06_sub</t:Uri></t:GetStreamUriResponse>'),
                     response('<t:GetDeviceInformationResponse><t:Manufacturer>Manufacturer</t:Manufacturer>'
                              '<t:Model>RLN8-410</t:Model></t:GetDeviceInformationResponse>'))
        cameras = ONVIFClient("recorder", "admin", "secret").cameras("id", "Home")
        self.assertEqual([camera.name for camera in cameras], ["Front Yard", "Pool North"])
        names.assert_called_once_with("http://recorder/onvif/device_service", "admin", "secret")

    @patch("lookout.onvif.camera_names", return_value={})
    def test_optional_names_cannot_break_playback(self, names):
        for information in (Reply(UNSUPPORTED, 500),
                            response('<t:GetDeviceInformationResponse><t:Model>Another vendor</t:Model></t:GetDeviceInformationResponse>'),
                            response('<t:GetDeviceInformationResponse><t:Model>RLN8-410</t:Model></t:GetDeviceInformationResponse>')):
            with self.subTest(information=information):
                names.reset_mock()
                self.replies(CLOCK, CAPABILITIES,
                             response(f'<t:GetProfilesResponse>{profile("011", name="VideoS_001")}</t:GetProfilesResponse>'),
                             response('<t:GetStreamUriResponse><t:Uri>rtsp://recorder/h264Preview_02_sub</t:Uri></t:GetStreamUriResponse>'),
                             information)
                cameras = ONVIFClient("recorder").cameras("id", "Home")
                self.assertEqual(len(cameras), 1)
                self.assertEqual(cameras[0].name, "VideoS_001")
                if isinstance(information, Reply) or b"Another vendor" in information:
                    names.assert_not_called()

    @patch("lookout.onvif.camera_names")
    def test_existing_friendly_names_do_not_use_vendor_api(self, names):
        cameras = self.media1(profile("011", name="Driveway"), "rtsp://recorder/h264Preview_02_sub")
        self.assertEqual(cameras[0].name, "Driveway")
        self.assertEqual(self.session.post.call_count, 4)
        names.assert_not_called()

    def test_profile_timeout_allows_slow_recorder(self):
        self.media1(profile("011"), "rtsp://recorder/live")
        profile_call = self.session.post.call_args_list[2]
        self.assertEqual(profile_call.kwargs["timeout"], (2.0, 8.0))

    def test_profile_and_channel_counts_are_bounded(self):
        self.media1("".join(profile(f"p{i}", source=f"s{i}") for i in range(100)), *[f"rtsp://recorder/{i}" for i in range(onvif.MAX_SOURCES)])
        self.assertEqual(self.session.post.call_count, 3 + onvif.MAX_SOURCES)
        self.session.reset_mock()
        cameras = self.media1("".join(profile(f"p{i}", width=1000 - i) for i in range(100)), "rtsp://recorder/sub")
        self.assertEqual(cameras[0].width, 1000 - (onvif.MAX_PROFILES - 1))

    def test_request_limit_and_deadline(self):
        client = ONVIFClient("recorder")
        client._deadline = 0
        with self.assertRaisesRegex(OnvifError, "limit"):
            client._post(client.address, "action", b"", None)
        client._deadline = float("inf")
        client._calls = onvif.MAX_CALLS
        with self.assertRaisesRegex(OnvifError, "limit"):
            client._post(client.address, "action", b"", None)
        self.session.post.assert_not_called()

    def test_error_messages_do_not_echo_credentials_or_fault_details(self):
        errors = [
            Reply(b"private-password", 401),
            Reply(b"private-password", 403),
            Reply(b"private-password", 302),
            Reply(b"private-password", 502),
            response('<s:Fault><s:Reason><s:Text>private-password http://admin:private-password@recorder</s:Text></s:Reason></s:Fault>'),
            requests.exceptions.ConnectionError("http://admin:private-password@recorder"),
            requests.exceptions.SSLError("private-password"),
            requests.exceptions.Timeout("private-password"),
            b"<broken private-password",
            b'<!DOCTYPE x [<!ENTITY e "private-password">]><x>&e;</x>',
            b"x" * (onvif.MAX_XML + 1),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.session.reset_mock()
                replies = self.replies(CLOCK, error)
                with self.assertRaises(OnvifError) as caught:
                    ONVIFClient("recorder", "admin", "private-password").cameras("id", "Home")
                self.assertNotIn("private-password", str(caught.exception))
                self.assertNotIn("admin", str(caught.exception))
                self.session.close.assert_called_once()
                for reply in replies:
                    if isinstance(reply, Reply):
                        self.assertTrue(reply.closed)

    def test_cross_host_media_endpoint_never_receives_credentials(self):
        capabilities = CAPABILITIES.replace(b"0.0.0.0", b"untrusted.example")
        self.replies(CLOCK, capabilities)
        with self.assertRaisesRegex(OnvifError, "another host"):
            ONVIFClient("recorder", "admin", "secret").cameras("id", "Home")
        self.assertEqual(self.session.post.call_count, 2)
        self.assertTrue(all(urlsplit(call.args[0]).hostname == "recorder" for call in self.session.post.call_args_list))

    def test_https_media_downgrade_rejected(self):
        self.replies(CLOCK, CAPABILITIES)
        with self.assertRaisesRegex(OnvifError, "insecure"):
            ONVIFClient("https://recorder", "admin", "secret").cameras("id", "Home")
        self.assertEqual(self.session.post.call_count, 2)

    def test_invalid_advertised_url_is_a_safe_onvif_error(self):
        self.replies(CLOCK, CAPABILITIES.replace(b"http://0.0.0.0", b"http://[private-password]"))
        with self.assertRaises(OnvifError) as caught:
            ONVIFClient("recorder").cameras("id", "Home")
        self.assertNotIn("private-password", str(caught.exception))

    def test_invalid_credentials_are_rejected_before_transport(self):
        for username, password in ((None, "secret"), ("user", None), ("user\x00", "secret"), ("user", "secret\ud800")):
            with self.assertRaises(OnvifError) as caught:
                ONVIFClient("recorder", username, password)
            self.assertNotIn("secret", str(caught.exception))
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
