import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lookout.firewall import DiscoveryPermission, FirewallError, FirewallSetup
from lookout.onvif import DISCOVERY_PORT
from lookout.storage import StorageError


SINCE = 1_800_000_000.25
RULE = DiscoveryPermission("wlp2s0", "172.16.1.0/24")
ALLOW = "-A ufw-user-input -i wlp2s0 -p udp -s 172.16.1.0/24 --sport 3702 --dport 37020 -j ACCEPT"


def interface(name="wlp2s0", address="172.16.1.24", prefix=24, **extra):
    return {"ifname": name, "flags": ["UP", "BROADCAST", "MULTICAST"], "link_type": "ether", "operstate": "UP",
            "addr_info": [{"family": "inet", "local": address, "prefixlen": prefix}], **extra}


def event(message=None, timestamp=SINCE + 1, **extra):
    return json.dumps({"__REALTIME_TIMESTAMP": str(int(timestamp * 1_000_000)), "_TRANSPORT": "kernel",
                       "MESSAGE": message if message is not None else
                       "[UFW BLOCK] IN=wlp2s0 OUT= MAC=00:11 SRC=172.16.1.200 DST=172.16.1.24 "
                       "LEN=120 PROTO=UDP SPT=3702 DPT=37020 LEN=100", **extra})


class FirewallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lookout-firewall-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "lookout" / "discovery.json"
        self.setup = FirewallSetup(self.path)
        self.files = {"/etc/ufw/ufw.conf": "ENABLED=yes\nLOGLEVEL=low\n",
                      "/etc/default/ufw": 'DEFAULT_INPUT_POLICY="DROP"\n', "/etc/ufw/user.rules": ""}
        self.interfaces = [interface()]
        self.routes = [{"dst": "default", "dev": "wlp2s0", "gateway": "172.16.1.1"}]
        self.journal = ""
        self.authorization = subprocess.CompletedProcess([], 0, "", "")
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if str(path).startswith("/etc/"):
                value = self.files.get(str(path), FileNotFoundError())
                if isinstance(value, Exception):
                    raise value
                return value
            return original_read(path, *args, **kwargs)

        patches = [patch("lookout.firewall.Path.read_text", autospec=True, side_effect=read),
                   patch("lookout.firewall.os.access", return_value=True),
                   patch("lookout.firewall.subprocess.run", side_effect=self.run_command)]
        self.read, self.access, self.run = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def run_command(self, argv, **kwargs):
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertFalse(kwargs["check"])
        if argv == ["ip", "-j", "-d", "-4", "addr", "show", "up"]:
            output = json.dumps(self.interfaces)
        elif argv == ["ip", "-j", "-4", "route", "show", "default"]:
            output = json.dumps(self.routes)
        elif argv[0] == "journalctl":
            output = self.journal
        elif argv[:2] == ["pkexec", "/usr/bin/ufw"]:
            self.assertEqual(kwargs["timeout"], 120)
            if isinstance(self.authorization, Exception):
                raise self.authorization
            return self.authorization
        else:
            self.fail(f"Unexpected command: {argv!r}")
        self.assertEqual(kwargs["timeout"], 5)
        if isinstance(output, Exception):
            raise output
        return subprocess.CompletedProcess(argv, 0, output, "")

    def assert_no_authorization(self):
        self.assertFalse(any(call.args[0][0] == "pkexec" for call in self.run.call_args_list))

    def test_correlated_kernel_block_confirms_exact_current_lan(self):
        self.journal = event() + "\n" + event()
        self.assertEqual(self.setup.check(SINCE), [DiscoveryPermission(RULE.interface, RULE.subnet, True)])
        self.assertEqual(DISCOVERY_PORT, 37020)
        journal = next(call for call in self.run.call_args_list if call.args[0][0] == "journalctl")
        self.assertEqual(journal.args[0], ["journalctl", "--kernel", "--boot", "--since", f"@{SINCE:.6f}",
                                         "--output=json", "--no-pager", "--quiet", "--lines=1000"])
        self.assert_no_authorization()
        self.assertFalse(self.path.parent.exists())

    def test_unrelated_or_old_logs_do_not_confirm(self):
        message = json.loads(event())["MESSAGE"]
        cases = [event(timestamp=SINCE - 0.01), event(_TRANSPORT="stdout"), event(MESSAGE=None),
                 event(__REALTIME_TIMESTAMP="nan"), "not-json", "[]", "null",
                 event(message.replace("[UFW BLOCK]", "[UFW ALLOW]"))]
        for old, new in (("IN=wlp2s0", "IN=eth0"), ("OUT= ", "OUT=eth0 "),
                         ("SPT=3702", "SPT=1234"), ("DPT=37020", "DPT=54321"),
                         ("PROTO=UDP", "PROTO=TCP"), ("SRC=172.16.1.200", "SRC=192.168.1.200"),
                         ("SRC=172.16.1.200", "SRC=172.16.1.24"), ("SRC=172.16.1.200", "SRC=172.16.1.255"),
                         ("DST=172.16.1.24", "DST=172.16.1.25"), ("DST=172.16.1.24", "DST=239.255.255.250")):
            cases.append(event(message.replace(old, new)))
        cases.append(event(message + " DPT=37020"))
        for log in cases:
            with self.subTest(log=log):
                self.journal = log
                self.assertEqual(self.setup.check(SINCE), [RULE])
        self.files["/etc/default/ufw"] = 'DEFAULT_INPUT_POLICY="ACCEPT"'
        self.journal = "\n".join(cases)
        self.assertEqual(self.setup.check(SINCE), [])

    def test_unreadable_missing_or_rate_limited_journal_falls_back(self):
        for log in ("", event("net_ratelimit: 10 callbacks suppressed"), PermissionError("private-data"),
                    FileNotFoundError(), subprocess.TimeoutExpired("journalctl", 5)):
            with self.subTest(log=log):
                self.journal = log
                self.assertEqual(self.setup.check(SINCE), [RULE])
        self.assert_no_authorization()
        self.assertFalse(self.path.exists())

    def test_fallback_needs_enabled_ufw_and_default_drop(self):
        for policy in ('DEFAULT_INPUT_POLICY="ACCEPT"', "DEFAULT_INPUT_POLICY=REJECT", "", PermissionError()):
            self.files["/etc/default/ufw"] = policy
            self.assertEqual(self.setup.check(SINCE), [])
        self.journal = event()
        self.assertEqual(self.setup.check(SINCE)[0].confirmed, True)
        for config in ("ENABLED=no", "# ENABLED=yes", "ENABLED=maybe", FileNotFoundError(), PermissionError()):
            self.files["/etc/ufw/ufw.conf"] = config
            self.assertEqual(self.setup.check(SINCE, force=True), [])
        self.files["/etc/ufw/ufw.conf"] = "ENABLED=yes"
        self.access.return_value = False
        self.assertEqual(self.setup.check(SINCE), [])
        self.assert_no_authorization()

    def test_lan_filter_excludes_vpns_containers_and_unusable_addresses(self):
        self.interfaces += [interface(name) for name in ("docker0", "veth123", "br-abc", "virbr0", "tailscale0", "tun0", "tap0", "wg0")]
        self.interfaces += [interface("enbad", linkinfo={"info_kind": kind}) for kind in ("wireguard", "tun", "veth", "bridge", "dummy")]
        self.interfaces += [interface("eth1", flags=["UP", "POINTOPOINT", "MULTICAST"]),
                            interface("eth2", operstate="DOWN"), interface("lo", link_type="loopback"),
                            interface("eth3", flags=["BROADCAST", "MULTICAST"])]
        self.interfaces += [interface("eth4", address, prefix) for address, prefix in
                            (("127.0.0.1", 8), ("100.64.1.2", 24), ("8.8.8.8", 24), ("0.0.0.0", 0),
                             ("224.0.0.2", 24), ("10.0.0.1", 32), ("172.16.1.0", 24), ("172.16.1.255", 24))]
        self.interfaces += [None, {"ifname": []}, interface("eth5", addr_info=None), interface("eth6", linkinfo=None)]
        self.assertEqual(self.setup.check(SINCE), [RULE])
        self.assert_no_authorization()

    def test_separate_lans_deduplicate_and_only_matching_network_is_confirmed(self):
        self.interfaces += [interface(), interface("enp3s0", "192.168.4.2"), interface("uplink", "10.3.4.2")]
        self.routes.append({"dst": "default", "dev": "uplink"})
        self.journal = event()
        self.assertEqual(self.setup.check(SINCE), [DiscoveryPermission(RULE.interface, RULE.subnet, True),
                                                 DiscoveryPermission("enp3s0", "192.168.4.0/24"),
                                                 DiscoveryPermission("uplink", "10.3.4.0/24")])

    def test_missing_or_malformed_ip_results_do_not_guess_a_network(self):
        for result in (FileNotFoundError(), subprocess.TimeoutExpired("ip", 5),
                       subprocess.CompletedProcess([], 1, "private-data", "private-data"),
                       subprocess.CompletedProcess([], 0, "not-json", ""),
                       subprocess.CompletedProcess([], 0, "{}", "")):
            self.run.side_effect = result if isinstance(result, Exception) else None
            self.run.return_value = result
            self.assertEqual(self.setup.check(SINCE), [])
        self.assert_no_authorization()

    def test_existing_simple_covering_accepts_suppress_even_forced_review(self):
        self.journal = event(timestamp=SINCE - 1)
        for allow in (ALLOW, ALLOW + ' -m comment --comment "Lookout ONVIF discovery"',
                      "-A ufw-user-input -p udp --dport 37020 -j ACCEPT",
                      "-A ufw-user-input -s 172.16.0.0/12 -j ACCEPT",
                      "-A ufw-user-input -i wlp+ -p udp -d 172.16.1.24/32 --dport 37020 -j ACCEPT",
                      "-A ufw-user-input -p 17 -m udp --sport 3702 --dport 37000:37100 -j ACCEPT",
                      "-A ufw-user-input -p udp -m multiport --dports 22,37020 --sports 3702 -j ACCEPT",
                      "-A ufw-user-input -p tcp --dport 22 -m conntrack --ctstate NEW -m recent --set\n" + ALLOW):
            with self.subTest(allow=allow):
                self.files["/etc/ufw/user.rules"] = "*filter\n" + allow + "\nCOMMIT\n"
                self.assertEqual(self.setup.check(SINCE), [])
                self.assertEqual(self.setup.check(SINCE, force=True), [])
        self.assert_no_authorization()

    def test_current_kernel_block_outweighs_possibly_stale_config_accept(self):
        self.files["/etc/ufw/user.rules"] = ALLOW
        self.journal = event()
        self.assertEqual(self.setup.check(SINCE), [DiscoveryPermission(RULE.interface, RULE.subnet, True)])

    def test_nonmatching_conditional_or_shadowed_accepts_do_not_hide_block(self):
        cases = [ALLOW.replace("ACCEPT", "DROP"), ALLOW.replace("udp", "tcp"),
                 ALLOW.replace("37020", "3702"), ALLOW.replace("--sport 3702", "--sport 1234"),
                 ALLOW.replace("wlp2s0", "eth0"), ALLOW.replace("172.16.1.0/24", "192.168.1.0/24"),
                 ALLOW.replace("172.16.1.0/24", "172.16.1.200/32"),
                 ALLOW + " -d 172.16.1.25", ALLOW + " -m conntrack --ctstate ESTABLISHED,RELATED",
                 ALLOW.replace("-s", "! -s"), ALLOW.replace("ufw-user-input", "ufw-user-forward"),
                 "-A ufw-user-input -s 172.16.1.200 -j DROP\n" + ALLOW,
                 "-A ufw-user-input -j REJECT\n" + ALLOW, PermissionError()]
        for rules in cases:
            with self.subTest(rules=rules):
                self.files["/etc/ufw/user.rules"] = rules
                self.assertEqual(self.setup.check(SINCE), [RULE])

    def test_remember_once_restart_force_review_and_new_network(self):
        self.assertEqual(self.setup.check(SINCE), [RULE])
        self.setup.remember(RULE)
        self.setup.remember(DiscoveryPermission(RULE.interface, RULE.subnet, True))
        self.assertEqual(RULE.key, DiscoveryPermission(RULE.interface, RULE.subnet, True).key)
        restarted = FirewallSetup(self.path)
        self.assertEqual(restarted.check(SINCE), [])
        self.assertEqual(restarted.check(SINCE, force=True), [RULE])
        self.assertEqual(json.loads(self.path.read_text()), {"version": 1, "reviewed": [RULE.key]})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.interfaces = [interface(address="192.168.4.2")]
        self.assertEqual(restarted.check(SINCE), [DiscoveryPermission("wlp2s0", "192.168.4.0/24")])
        self.assert_no_authorization()

    def test_atomic_failure_preserves_decision_and_cleans_temporary_file(self):
        self.setup.remember(RULE)
        before = self.path.read_bytes()
        with patch("lookout.firewall.os.replace", side_effect=OSError("private-data")):
            with self.assertRaises(FirewallError) as caught:
                self.setup.remember(DiscoveryPermission("eth0", "192.168.1.0/24"))
        self.assertNotIn("private-data", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_state_path_defaults_and_invalid_state(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.directory.name}):
            self.assertEqual(FirewallSetup().state_path, self.path)
        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative"}), patch("lookout.firewall.Path.home", return_value=Path(self.directory.name)):
            self.assertEqual(FirewallSetup().state_path, Path(self.directory.name) / ".local/state/lookout/discovery.json")
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir()
        for raw in ("not-json", "[]", '{"version":true,"reviewed":[]}', '{"version":1,"reviewed":[{}]}', "x" * 65537):
            self.path.write_text(raw)
            with self.assertRaises(FirewallError):
                self.setup.check(SINCE)
            self.assertEqual(self.setup.check(SINCE, force=True), [RULE])
            with self.assertRaises(FirewallError):
                self.setup.remember(RULE)

    def test_symlink_state_directory_is_not_written(self):
        target = Path(self.directory.name) / "target"
        target.mkdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(FirewallError):
            self.setup.remember(RULE)
        self.assertEqual(list(target.iterdir()), [])

    def test_allow_exact_argv_revalidates_and_never_remembers(self):
        self.assertIsNone(self.setup.allow(DiscoveryPermission(RULE.interface, RULE.subnet, True)))
        self.assertEqual(self.run.call_args.args[0], ["pkexec", "/usr/bin/ufw", "allow", "in", "on", "wlp2s0",
                                                    "proto", "udp", "from", "172.16.1.0/24", "port", "3702", "to", "any",
                                                     "port", "37020", "comment", "Camera Panel ONVIF discovery"])
        self.assertEqual([call.args[0][0] for call in self.run.call_args_list], ["ip", "ip", "pkexec"])
        self.assertFalse(self.path.exists())
        self.setup.remember(RULE)
        self.assertEqual(self.setup.check(SINCE), [])

    def test_network_changes_prevent_authorization(self):
        for interfaces in ([], [interface("eth0")], [interface(address="192.168.1.2")],
                           [interface(prefix=16)], [interface(linkinfo={"info_kind": "tun"})]):
            self.interfaces = interfaces
            with self.assertRaisesRegex(FirewallError, "network changed"):
                self.setup.allow(RULE)
        self.assert_no_authorization()
        self.assertFalse(self.path.exists())

    def test_auth_cancel_failure_timeout_or_unavailable_never_remembers(self):
        failures = [subprocess.CompletedProcess([], code, "private-data", "private-data") for code in (1, 126, 127)]
        failures += [subprocess.TimeoutExpired("private-data", 120), FileNotFoundError("private-data"),
                     PermissionError("private-data")]
        for failure in failures:
            with self.subTest(failure=failure):
                self.authorization = failure
                with self.assertRaises(FirewallError) as caught:
                    self.setup.allow(RULE)
                self.assertIsInstance(caught.exception, StorageError)
                self.assertNotIn("private-data", str(caught.exception))
                self.assertFalse(self.path.exists())
                self.assertEqual(self.setup.check(SINCE), [RULE])

    def test_unavailable_or_disabled_ufw_cannot_be_allowed(self):
        self.access.return_value = False
        with self.assertRaisesRegex(FirewallError, "unavailable or disabled"):
            self.setup.allow(RULE)
        self.access.return_value = True
        self.files["/etc/ufw/ufw.conf"] = "ENABLED=no"
        with self.assertRaises(FirewallError):
            self.setup.allow(RULE)
        self.assert_no_authorization()

    def test_malicious_or_overbroad_permissions_rejected_before_commands(self):
        rules = [DiscoveryPermission(name, RULE.subnet) for name in
                 ("--help", "wlp2s0;id", "wlp2s0\n", "wlp2s0+", "../wlp2s0", "x" * 16, "", None)]
        rules += [DiscoveryPermission(RULE.interface, subnet) for subnet in
                  ("0.0.0.0/0", "::/0", "127.0.0.0/8", "224.0.0.0/4", "172.16.1.5/24",
                   "172.16.1.0/255.255.255.0", "172.16.1.0/24;id", "172.16.1.0/32", "--help", None)]
        rules += [None, "private-data"]
        for rule in rules:
            with self.subTest(rule=rule):
                with self.assertRaises(FirewallError):
                    self.setup.allow(rule)
                with self.assertRaises(FirewallError):
                    self.setup.remember(rule)
        self.run.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_invalid_scan_time_rejected_without_commands(self):
        for since in (-1, float("nan"), float("inf"), 10 ** 1000, True, "--help", None):
            with self.assertRaises(FirewallError):
                self.setup.check(since)
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
