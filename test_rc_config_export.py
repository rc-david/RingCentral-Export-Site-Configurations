"""Offline tests: no real credentials, API calls, or browser launches."""

import argparse
import copy
import csv
from email.message import Message
from io import BytesIO, StringIO
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit


SPEC = importlib.util.spec_from_file_location("rc_config_export", Path(__file__).with_name("rc-config-export.py"))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)
BASE = app.BASE
SITES = [{"id": "main-site", "name": "Main Site"}, {"id": 101, "name": "London", "extensionNumber": "10"},
         {"id": 202, "name": "New York", "extensionNumber": "20"}]
RULES = [{"id": "business-hours-rule", "type": "BusinessHours"}, {"id": "custom/1", "type": "Custom", "name": "Holiday", "enabled": True},
         {"id": "custom2", "type": "Custom", "name": "Disabled rule", "enabled": False},
         {"id": "after-hours-rule", "type": "AfterHours"}]
FULL_RULE = {**RULES[1], "calledNumbers": [{"phoneNumber": "+15550000001"}],
             "callers": [{"callerId": "+15550000002", "name": "Sample caller"}],
             "schedule": {"weeklyRanges": {"monday": [{"from": "09:00", "to": "17:00"}]}},
             "callHandlingAction": "ForwardCalls",
             "forwarding": {"rules": [{"forwardingNumbers": [{"phoneNumber": "+15550000003"}], "ringCount": 4}]},
             "voicemail": {"enabled": True, "recipient": {"extensionNumber": "30"}},
             "greetings": [{"type": "Intro", "custom": {"id": "greeting1"}}],
             "additionalField": {"nested": [1, {"preserve": "everything"}]}}
MENUS = [{"id": 501, "name": "London menu", "site": {"id": 101}},
         {"id": 502, "name": "New York menu", "site": {"id": 202}},
         {"id": 503, "name": "Main menu"}]
LOCATIONS = [{"id": 601, "name": "London emergency", "site": {"id": 101}},
             {"id": 602, "name": "New York emergency", "site": {"id": 202}}]


class QuietLog:
    def __init__(self):
        self.warnings = []

    def info(self, message):
        pass

    def warn(self, message):
        self.warnings.append(message)


def opts(*args):
    return app.parse_args(list(args))


def paginate(records, query):
    page = query.get("page", 1)
    return {"records": copy.deepcopy(records[page - 1:page]), "paging": {"totalPages": max(1, len(records))}}


class FixtureClient(app.RcClient):
    def __init__(self, overrides=None):
        super().__init__("https://example.invalid", QuietLog())
        self.overrides = overrides or {}
        self.calls = []

    def login(self, force=False):
        self.token = "mock"

    def get(self, path, query=None):
        query = query or {}
        self.calls.append((path, query))
        self.requests += 1
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception):
                raise value
            data = value(query) if callable(value) else copy.deepcopy(value)
        elif path == BASE:
            data = {"id": 1, "name": "Demo", "mainNumber": "+15550000000"}
        elif path == "/restapi/v2/accounts/~/regional-settings":
            data = {"timezone": {"id": "utc", "name": "UTC"}, "timeFormat": "24h"}
        elif path == f"{BASE}/sites":
            data = paginate(SITES, query)
        elif path == f"{BASE}/ivr-menus":
            data = paginate(MENUS, query)
        elif path == f"{BASE}/emergency-locations":
            data = paginate(LOCATIONS, query)
        elif path == f"{BASE}/extension":
            data = paginate([{"id": "m1", "name": "Main member"}, {"id": "m2", "site": {"id": 101}}], query)
        elif path.endswith("/members"):
            data = paginate([{"id": "u1", "name": "Sample user"}, {"id": "q1", "name": "Sample queue", "type": "Department"}], query)
        elif path.endswith("/answering-rule"):
            data = paginate(RULES, query)
        elif "/answering-rule/" in path:
            from urllib.parse import unquote
            rule_id = unquote(path.rsplit("/", 1)[-1])
            data = copy.deepcopy(FULL_RULE) if rule_id == "custom/1" else {
                **next(r for r in RULES if r["id"] == rule_id), "fullField": {"nested": "retained"},
            }
        elif path.endswith("/business-hours"):
            data = {"schedule": {}}
        elif path.endswith("/phone-number"):
            data = paginate([{"id": "n1", "phoneNumber": "+15550000001"},
                             {"id": "n2", "phoneNumber": "+15550000002", "site": {"id": 101}}], query)
        elif path.endswith("/ivr"):
            data = {"topMenu": {"id": 501, "extensionNumber": "50"}}
        elif "/ivr-menus/" in path:
            menu_id = int(path.rsplit("/", 1)[-1])
            data = {**next(m for m in MENUS if m["id"] == menu_id),
                    "actions": [{"input": "1", "action": "Connect", "extension": {"id": "u1"}}],
                    "prompt": {"mode": "TextToSpeech", "text": "Welcome"}}
        elif "/emergency-locations/" in path:
            location_id = int(path.rsplit("/", 1)[-1])
            data = {**next(l for l in LOCATIONS if l["id"] == location_id), "address": {"street": "Example Street", "city": "Example City"}}
        elif "/sites/" in path or "/extension/" in path:
            data = {"settings": {"regionalSettings": {"timezone": {"name": "UTC"}}}}
        else:
            raise AssertionError(f"Unexpected API request: {path}")
        self.responses.append({"path": path, "query": query, "data": copy.deepcopy(data)})
        return data


def export(client, *args):
    ctx = {"errors": [], "notes": [], "log": client.log}
    sites = app.export_sites(client, opts(*(args or ("--all-sites",))), ctx)
    return sites, ctx


def report_payload(report):
    match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', report, re.S)
    return json.loads(match.group(1))


def large_report_result(count=1450):
    sites, ctx = export(FixtureClient(), "--site-id", "101", "--site-id", "202")
    for site_index, site in enumerate(sites):
        rules = []
        statuses = []
        indices = range(0, count // 2) if site_index == 0 else range(count // 2, count)
        for index in indices:
            rule = copy.deepcopy(FULL_RULE)
            rule.update(id=f"rule-{index}", name=f"Rule {index:04d}", enabled=index % 2 == 0)
            rule["callers"][0]["name"] = f"Caller {index}"
            if index % 10 == 0:
                rule["type"] = "BusinessHours"
            rule["transfer"] = {"extension": {"id": f"target-{index}", "uri": f"https://example.invalid/extension/{index}"}}
            rules.append(rule)
            statuses.append({"id": rule["id"], "status": "incomplete" if index % 17 == 0 else "complete",
                             "error": {"message": f"Permission warning {index}"} if index % 17 == 0 else None})
        site["answeringRules"] = rules
        site["answeringRuleDetails"] = statuses
        site["customRules"] = [r for r in rules if r["type"] == "Custom"]
        site["customRuleDetails"] = [d for r, d in zip(rules, statuses) if r["type"] == "Custom"]
    return {"account": {"id": "demo", "name": "Large offline demo"}, "finishedAt": "Synthetic example",
            "sites": sites, "counts": {"sites": 2, "answeringRules": count}, "errors": [], "notes": []}


class SelectionTests(unittest.TestCase):
    def test_explicit_scope_and_help(self):
        with patch("sys.stderr", new=StringIO()), patch("sys.stdout", new=StringIO()):
            for args in [[], ["--all-sites", "--site-id", "101"], ["--site-id", ""], ["--site-name", " "],
                         ["--site-id"], ["--queues-only"], ["--sites-only"], ["--no-custom-rules"]]:
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    opts(*args)
                self.assertEqual(error.exception.code, 2)
            with self.assertRaises(SystemExit) as error:
                opts("--help")
            self.assertEqual(error.exception.code, 0)
        self.assertFalse(opts("--all-sites", "--no-open").open)
        self.assertTrue(opts("--all-sites").open)

    def test_exact_names_ids_and_union(self):
        options = opts("--site-id", "101", "--site-id", "202", "--site-name", "London", "--site-name", "London")
        self.assertEqual([str(s["id"]) for s in app.select_sites(SITES, options)], ["101", "202"])
        self.assertEqual(len(opts("--all-sites").site_ids), 0)
        for flag, value in [("--site-id", "missing"), ("--site-id", "10"), ("--site-name", "london")]:
            with self.assertRaisesRegex(app.ExportError, "unknown site"):
                app.select_sites(SITES, opts(flag, value))

    def test_ambiguous_names_and_programmatic_validation(self):
        with self.assertRaisesRegex(app.ExportError, "ambiguous.*--site-id"):
            app.select_sites([{"id": 1, "name": "Same"}, {"id": 2, "name": "Same"}], opts("--site-name", "Same"))
        options = argparse.Namespace(all_sites=False, site_ids=[], site_names=[])
        with self.assertRaises(app.ExportError):
            app.validate_selection(options)

    def test_unknown_selector_before_optional_endpoint_calls(self):
        client = FixtureClient()
        with self.assertRaises(app.ExportError):
            export(client, "--site-id", "missing")
        self.assertTrue(all(path == f"{BASE}/sites" for path, _ in client.calls))


class CollectionTests(unittest.TestCase):
    def test_all_site_pages_and_all_rule_details(self):
        client = FixtureClient()
        sites, ctx = export(client)
        self.assertEqual([s["id"] for s in sites], ["main-site", "101", "202"])
        self.assertEqual([q["page"] for p, q in client.calls if p == f"{BASE}/sites"], [1, 2, 3])
        for site in sites:
            self.assertEqual(site["customRules"][0], FULL_RULE)
            self.assertFalse(site["customRules"][1]["enabled"])
            self.assertEqual(len(site["answeringRules"]), 4)
            self.assertIn("fullField", site["answeringRules"][0])
            self.assertIn("fullField", site["answeringRules"][3])
            self.assertTrue(all(s["status"] == "complete" for s in site["answeringRuleDetails"]))
        self.assertFalse(ctx["errors"])
        self.assertTrue(any(p.endswith("/answering-rule/custom%2F1") for p, _ in client.calls))
        self.assertTrue(all(q["view"] == "Detailed" for p, q in client.calls if p.endswith("/answering-rule")))
        self.assertEqual([m["id"] for m in sites[0]["members"]], ["m1"])

    def test_selected_site_only_and_paginated_members(self):
        client = FixtureClient()
        sites, _ = export(client, "--site-id", "101", "--site-name", "London")
        self.assertEqual(len(sites), 1)
        self.assertEqual(len(sites[0]["members"]), 2)
        paths = [p for p, _ in client.calls]
        self.assertFalse(any("/202" in p or "/ivr-menus/502" in p or "/emergency-locations/602" in p for p in paths))
        self.assertFalse(any("/call-queues" in p or "/extension/u1" in p or "/extension/q1" in p for p in paths))

    def test_menu_and_emergency_details(self):
        client = FixtureClient()
        sites, _ = export(client, "--site-id", "101")
        site = sites[0]
        self.assertEqual(site["ivrMenus"][0]["actions"][0]["input"], "1")
        self.assertEqual(site["ivrMenus"][0]["prompt"]["text"], "Welcome")
        self.assertEqual(site["emergencyLocations"][0]["address"]["city"], "Example City")
        self.assertTrue(all(d["status"] == "complete" for d in site["ivrMenuDetails"] + site["emergencyLocationDetails"]))
        self.assertEqual(len(site["phoneNumbers"]), 2)
        self.assertEqual([q["page"] for p, q in client.calls if p == f"{BASE}/ivr-menus"], [1, 2, 3])

    def test_main_site_does_not_use_invalid_extension(self):
        client = FixtureClient()
        sites, _ = export(client, "--site-name", "Main Site")
        self.assertTrue(sites[0]["mainSite"])
        self.assertFalse(sites[0]["synthetic"])
        paths = [p for p, _ in client.calls]
        self.assertIn(f"{BASE}/ivr", paths)
        self.assertIn(f"{BASE}/answering-rule/business-hours-rule", paths)
        self.assertFalse(any("/extension/main-site" in p or "/sites/main-site" in p for p in paths))
        self.assertEqual(len(sites[0]["phoneNumbers"]), 1)

    def test_synthetic_fallback_is_explicitly_incomplete(self):
        client = FixtureClient({f"{BASE}/sites": app.RcError(403, {"message": "No multi-site access"}, f"{BASE}/sites")})
        sites, ctx = export(client, "--site-id", "main-site")
        self.assertTrue(sites[0]["synthetic"])
        self.assertEqual(len(sites[0]["members"]), 2)
        self.assertEqual(len(sites[0]["ivrMenus"]), 3)
        self.assertEqual(len(sites[0]["emergencyLocations"]), 2)
        self.assertEqual(len(ctx["errors"]), 1)
        self.assertIn("does not prove", ctx["notes"][0])
        with self.assertRaisesRegex(app.ExportError, "unknown site"):
            export(client, "--site-id", "101")

    def test_discovery_second_page_failure_is_fatal(self):
        def fail(query):
            if query["page"] == 2:
                raise app.RcError(403, {"message": "Second page denied"}, f"{BASE}/sites")
            return paginate(SITES, query)
        with self.assertRaisesRegex(app.RcError, "Second page denied"):
            export(FixtureClient({f"{BASE}/sites": fail}))

    def test_detail_failures_retain_listings(self):
        path = f"{BASE}/extension/101/answering-rule/custom%2F1"
        for failure in [app.RcError(403, {"message": "Denied"}, path), {}, {"id": "wrong"}]:
            with self.subTest(failure=failure):
                sites, ctx = export(FixtureClient({path: failure}), "--site-id", "101")
                self.assertEqual(sites[0]["customRules"][0], RULES[1])
                self.assertEqual(sites[0]["customRuleDetails"][0]["status"], "incomplete")
                self.assertEqual(sites[0]["customRuleDetails"][1]["status"], "complete")
                self.assertTrue(ctx["errors"])

    def test_rule_discovery_failure_is_unknown_not_zero(self):
        path = f"{BASE}/extension/101/answering-rule"
        def fail(query):
            if query["page"] == 2:
                raise app.RcError(403, {"message": "Rule listing denied"}, path)
            return paginate(RULES, query)
        sites, ctx = export(FixtureClient({path: fail}), "--site-id", "101")
        self.assertIsNone(sites[0]["answeringRules"])
        self.assertIsNone(sites[0]["customRules"])
        self.assertEqual(sites[0]["customRuleListingStatus"], "incomplete")
        self.assertIn("unknown, not zero", ctx["notes"][0])

    def test_top_menu_can_be_read_when_menu_listing_is_denied(self):
        path = f"{BASE}/ivr-menus"
        sites, ctx = export(FixtureClient({path: app.RcError(403, {}, path)}), "--site-id", "101")
        self.assertIsNone(sites[0]["ivrMenuListing"])
        self.assertEqual(sites[0]["ivrMenus"][0]["id"], 501)
        self.assertEqual(sites[0]["dataStatus"]["ivrMenus"]["status"], "incomplete")
        self.assertTrue(ctx["errors"])

    def test_optional_failure_keeps_other_site_data(self):
        path = f"{BASE}/emergency-locations"
        sites, ctx = export(FixtureClient({path: app.RcError(403, {}, path)}), "--site-id", "101")
        self.assertIsNone(sites[0]["emergencyLocations"])
        self.assertEqual(sites[0]["customRules"][0], FULL_RULE)
        self.assertTrue(ctx["errors"])

    def test_menu_and_location_detail_failures_mark_collections_incomplete(self):
        for field, path, statuses in [
            ("ivrMenus", f"{BASE}/ivr-menus/501", "ivrMenuDetails"),
            ("emergencyLocations", f"{BASE}/emergency-locations/601", "emergencyLocationDetails"),
        ]:
            with self.subTest(field=field):
                sites, ctx = export(FixtureClient({path: app.RcError(403, {}, path)}), "--site-id", "101")
                self.assertEqual(sites[0]["dataStatus"][field]["status"], "incomplete")
                self.assertEqual(sites[0][statuses][0]["status"], "incomplete")
                self.assertEqual(sites[0][field][0]["name"], MENUS[0]["name"] if field == "ivrMenus" else LOCATIONS[0]["name"])
                self.assertTrue(ctx["errors"])


class FakeResponse:
    def __init__(self, data, status=200, headers=None):
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.data


class NumberRoutingTests(unittest.TestCase):
    SITE = {"id": "1", "name": "Dental", "members": [{"id": "10", "name": "Denver Ring Group", "extensionNumber": "1001", "type": "Department"}],
            "customRules": [
                {"id": "r1", "name": "1001 - Denver", "enabled": True, "callHandlingAction": "TransferToExtension",
                 "calledNumbers": [{"phoneNumber": "+17203985509"}, {"phoneNumber": "+17203985510"}], "transfer": {"extension": {"id": 10}}},
                {"id": "r2", "name": "Unknown target", "enabled": False, "callHandlingAction": "TransferToExtension",
                 "calledNumbers": [{"phoneNumber": "+15550000009"}], "transfer": {"extension": {"id": 99}}},
                {"id": "r3", "name": "Forward", "enabled": True, "callHandlingAction": "ForwardCalls",
                 "forwarding": {"rules": [{"forwardingNumbers": [{"phoneNumber": "+15550000003"}]}]}}]}

    def test_rows_per_called_number_with_resolved_target(self):
        rows = app.number_routing_rows([self.SITE])
        self.assertEqual([r["calledNumber"] for r in rows], ["+17203985509", "+17203985510", "+15550000009", ""])
        self.assertEqual((rows[0]["targetName"], rows[0]["targetExtensionNumber"], rows[0]["targetType"], rows[0]["targetStatus"]),
                         ("Denver Ring Group", "1001", "Department", "resolved"))
        self.assertIn("not found", rows[2]["targetStatus"])
        self.assertEqual(rows[2]["targetExtensionId"], "99")
        self.assertEqual(rows[3]["forwardToNumbers"], "+15550000003")
        self.assertEqual(rows[3]["targetStatus"], "")

    def test_report_has_number_routing_tab_and_data(self):
        site = {**self.SITE, "dataStatus": {"answeringRules": {"status": "complete"}}}
        report = app.render_report({"sites": [site]}, [])
        self.assertIn('data-category="numberRouting"', report)
        routing = report_payload(report)["sites"][0]["numberRouting"]
        self.assertEqual([r["name"] for r in routing][:2], ["+17203985509", "+17203985510"])
        self.assertEqual(routing[0]["targetName"], "Denver Ring Group")
        self.assertEqual(report_payload(report)["sites"][0]["dataStatus"]["numberRouting"]["status"], "complete")

    def test_account_extension_list_resolves_targets_outside_the_site(self):
        self.assertEqual(app.unresolved_target_ids([self.SITE]), {"99"})
        rows = app.number_routing_rows([self.SITE], [{"id": 99, "name": "Elsewhere", "extensionNumber": "2000"}])
        self.assertEqual(rows[2]["targetName"], "Elsewhere")


class RingGroupDetailTests(unittest.TestCase):
    DEVICES = [{"id": "d1", "type": "HardPhone", "name": "1059 1", "serial": "AA:BB:CC:00:00:01", "status": "Online",
                "model": {"name": "Poly VVX 450"}, "phoneLines": [{"lineType": "Standalone", "phoneInfo": {"phoneNumber": "+14806137524"}}]},
               {"id": "d2", "type": "HardPhone", "name": "Spare", "serial": "AA:BB:CC:00:00:09", "phoneLines": []}]
    V1_RULE = {"forwarding": {"ringingMode": "Simultaneously", "rules": [
        {"index": 1, "ringCount": 4, "enabled": True,
         "forwardingNumbers": [{"label": "1059 1", "type": "PhoneLine", "phoneNumber": "(480) 613-7524"}]}]}}
    V2_RULE = {"dispatching": {"type": "RingAtOnce", "actions": [
        {"type": "PlayWelcomePromptAction", "enabled": False},
        {"type": "RingGroupAction", "enabled": True, "duration": 20,
         "targets": [{"type": "CoworkerRingTarget", "extension": {"id": "55"}}]},
        {"type": "RingGroupAction", "enabled": True, "duration": 30, "targets": [{"type": "AllDesktopRingTarget", "name": "My desktop"}]}]}}
    MEMBER_DEVICES = [{"id": "m1", "type": "HardPhone", "name": "Front desk", "serial": "11:22:33:44:55:66", "model": {"name": "Yealink T54W"},
                       "phoneLines": [{"phoneInfo": {"phoneNumber": "+15551230001"}}]}]

    def v1_group(self):
        return {"extensionId": "10", "extension": {"name": "Queen Creek 6 Ring Group", "extensionNumber": "1011059", "type": "User"},
                "devices": self.DEVICES, "ringRule": self.V1_RULE, "ringRuleSource": "v1", "memberDetails": {}, "errors": [], "status": "complete"}

    def v2_group(self):
        member = {"extension": {"id": "55", "name": "Pat Jones", "extensionNumber": "105", "type": "User"}, "devices": self.MEMBER_DEVICES, "errors": []}
        return {"extensionId": "10", "extension": {"name": "Front Ring Group", "extensionNumber": "1000", "type": "User"},
                "devices": [], "ringRule": self.V2_RULE, "ringRuleSource": "v2", "memberDetails": {"55": member}, "errors": [], "status": "complete"}

    def test_v1_rows_join_ring_entries_to_devices_and_keep_unlisted_devices(self):
        rows = app.ring_group_rows([self.v1_group()], [{"targetExtensionId": "10", "ruleName": "1059 - Queen Creek"}])
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["ringGroupName"], rows[0]["ringEntryNumber"], rows[0]["deviceName"], rows[0]["serialOrMac"], rows[0]["deviceDid"]),
                         ("Queen Creek 6 Ring Group", "(480) 613-7524", "1059 1", "AA:BB:CC:00:00:01", "+14806137524"))
        self.assertEqual(rows[0]["usedByRules"], "1059 - Queen Creek")
        self.assertEqual((rows[1]["deviceName"], rows[1]["memberKind"]), ("Spare", "Ring group's own device"))

    def test_v2_rows_expand_coworkers_to_their_devices(self):
        rows = app.ring_group_rows([self.v2_group()], [])
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["memberKind"], rows[0]["memberName"], rows[0]["memberExtensionNumber"], rows[0]["deviceName"],
                          rows[0]["serialOrMac"], rows[0]["deviceDid"], rows[0]["ringOrder"], rows[0]["ringDurationSeconds"], rows[0]["ringingMode"]),
                         ("Co-worker", "Pat Jones", "105", "Front desk", "11:22:33:44:55:66", "+15551230001", 1, 20, "RingAtOnce"))
        self.assertEqual((rows[1]["memberKind"], rows[1]["memberName"], rows[1]["ringOrder"]), ("Desktop apps", "My desktop", 2))

    def test_fetch_prefers_v2_then_reads_member_devices(self):
        base = f"{BASE}/extension/10"
        v2 = f"{app.V2}/10/comm-handling/voice/state-rules/work-hours"
        client = FixtureClient({base: {"id": "10", "name": "Ring", "type": "User"}, f"{base}/device": lambda q: paginate([], q), v2: self.V2_RULE,
                                f"{BASE}/extension/55": {"id": "55", "name": "Pat Jones", "type": "User"},
                                f"{BASE}/extension/55/device": lambda q: paginate(self.MEMBER_DEVICES, q)})
        ctx = {"errors": [], "notes": [], "log": client.log}
        group = app.fetch_ring_groups(client, ctx, {"10"})[0]
        self.assertEqual((group["status"], group["ringRuleSource"]), ("complete", "v2"))
        self.assertEqual(group["memberDetails"]["55"]["devices"][0]["serial"], "11:22:33:44:55:66")
        self.assertEqual(ctx["errors"], [])

    def test_fetch_falls_back_to_v1_and_reports_failures_without_stopping(self):
        base = f"{BASE}/extension/10"
        v2 = f"{app.V2}/10/comm-handling/voice/state-rules/work-hours"
        client = FixtureClient({base: {"id": "10", "type": "User"}, f"{base}/device": app.RcError(403, {}, base), v2: app.RcError(403, {}, v2),
                                f"{base}/answering-rule/business-hours-rule": self.V1_RULE})
        ctx = {"errors": [], "notes": [], "log": client.log}
        group = app.fetch_ring_groups(client, ctx, {"10"})[0]
        self.assertEqual(group["ringRuleSource"], "v1")
        self.assertTrue(group["status"].startswith("incomplete"))
        self.assertEqual(len(ctx["errors"]), 1)
        self.assertEqual(len(app.ring_group_rows([group], [])), 1)

    def test_ivr_targets_are_skipped_cleanly(self):
        base = f"{BASE}/extension/10"
        client = FixtureClient({base: {"id": "10", "type": "IvrMenu", "name": "Menu"}})
        ctx = {"errors": [], "notes": [], "log": client.log}
        group = app.fetch_ring_groups(client, ctx, {"10"})[0]
        self.assertEqual((group["status"], ctx["errors"]), ("complete", []))

    def test_report_carries_extension_names_for_destinations(self):
        site = {**NumberRoutingTests.SITE, "dataStatus": {"answeringRules": {"status": "complete"}}}
        site["answeringRules"] = site["customRules"]
        data = report_payload(app.render_report({"sites": [site]}, []))
        self.assertEqual(data["extensionNames"]["10"], {"name": "Denver Ring Group", "extensionNumber": "1001", "type": "Department"})
        self.assertNotIn("99", data["extensionNames"])
        self.assertFalse(data["ringGroupDetailsRequested"])

    def test_flag_is_off_by_default_and_report_gets_tab_data(self):
        self.assertFalse(opts("--all-sites").ring_group_details)
        self.assertTrue(opts("--all-sites", "--ring-group-details").ring_group_details)
        site = {**NumberRoutingTests.SITE, "dataStatus": {"answeringRules": {"status": "complete"}}}
        site["customRules"] = [{**site["customRules"][0], "transfer": {"extension": {"id": 10}}}]
        report = app.render_report({"sites": [site], "ringGroups": [self.v2_group()]}, [])
        details = report_payload(report)["sites"][0]["ringGroupDetails"]
        self.assertEqual(details[0]["serialOrMac"], "11:22:33:44:55:66")
        self.assertNotIn("ringGroups", report_payload(report))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.now = 0
        self.waits = []

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def client(self, opener):
        return app.RcClient("https://example.invalid", QuietLog(), opener=opener, sleep=self.sleep, clock=lambda: self.now)

    def test_jwt_login_and_credentials_are_not_recorded(self):
        os.environ.update(RC_CLIENT_ID="example-client", RC_CLIENT_SECRET="example-secret", RC_JWT="example-jwt")
        calls = []
        def opener(request, timeout):
            calls.append(request)
            return FakeResponse({"access_token": "mock-token", "owner_id": 1, "expires_in": 3600})
        client = self.client(opener)
        client.login()
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(parse_qs(calls[0].data.decode())["assertion"], ["example-jwt"])
        self.assertEqual(client.token, "mock-token")
        self.assertEqual(client.responses, [])

    def test_access_token_skips_login_and_missing_credentials_fail(self):
        os.environ["RC_ACCESS_TOKEN"] = "mock-token"
        client = self.client(lambda *a, **k: self.fail("login should be skipped"))
        client.login()
        self.assertEqual(client.token, "mock-token")
        del os.environ["RC_ACCESS_TOKEN"]
        with self.assertRaisesRegex(app.ExportError, "required"):
            self.client(lambda *a, **k: None).login()

    def test_get_queries_and_verbatim_response(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            return FakeResponse({"id": "data", "nested": {"x": [1, 2]}})
        client = self.client(opener)
        client.token = "mock-token"
        data = client.get("/resource", {"page": 2, "name": "A & B"})
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(parse_qs(urlsplit(calls[0].full_url).query)["name"], ["A & B"])
        self.assertEqual(client.responses[0]["data"], data)
        self.assertNotIn("mock-token", json.dumps(client.responses))

    def test_http_error_is_reported(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 403, "Denied", Message(), BytesIO(b'{"errorCode":"CMN-419","message":"Extended access required"}'))
        with self.assertRaisesRegex(app.RcError, "CMN-419.*Extended access"):
            self.client(opener).get("/resource")

    def test_retry_429_and_server_errors(self):
        responses = iter([FakeResponse({}, 429, {"Retry-After": 2}), FakeResponse({}, 503), FakeResponse({"id": "ok"})])
        client = self.client(lambda *a, **k: next(responses))
        self.assertEqual(client.get("/resource")["id"], "ok")
        self.assertEqual(client.retries, 2)
        self.assertEqual(self.waits, [2.25, 3.0])

    def test_retry_exhaustion_is_reported(self):
        client = self.client(lambda *a, **k: FakeResponse({}, 429, {"Retry-After": 1}))
        with self.assertRaises(app.RcError) as error:
            client.get("/resource")
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(client.requests, 6)
        self.assertEqual(client.retries, 5)

    def test_network_retry_and_limit(self):
        def fail(request, timeout):
            raise URLError("offline")
        client = self.client(fail)
        with self.assertRaisesRegex(app.ExportError, "network error"):
            client.get("/resource")
        self.assertEqual(client.requests, 4)
        self.assertEqual(client.retries, 3)

    def test_401_refreshes_only_once(self):
        os.environ.update(RC_CLIENT_ID="client", RC_CLIENT_SECRET="secret", RC_JWT="jwt")
        responses = iter([FakeResponse({}, 401), FakeResponse({"access_token": "renewed"}), FakeResponse({}, 401)])
        client = self.client(lambda *a, **k: next(responses))
        with self.assertRaises(app.RcError):
            client.get("/resource")
        self.assertEqual(client.token, "renewed")
        self.assertEqual(client.retries, 1)

    def test_rate_limit_group_wait(self):
        responses = iter([FakeResponse({"id": "1"}, headers={"X-Rate-Limit-Group": "light", "X-Rate-Limit-Remaining": 0, "X-Rate-Limit-Window": 3}),
                          FakeResponse({"id": "2"})])
        client = self.client(lambda *a, **k: next(responses))
        client.get(f"{BASE}/extension/101/answering-rule")
        client.get(f"{BASE}/extension/202/answering-rule")
        self.assertEqual(self.waits, [3.25])

    def test_pagination_navigation_limits_and_invalid_records(self):
        client = self.client(lambda *a, **k: None)
        client.get = lambda p, q: {"records": [{"id": q["page"]}], "navigation": {"nextPage": {"uri": "next"}} if q["page"] < 3 else {}}
        self.assertEqual([r["id"] for r in client.get_all("/list")], [1, 2, 3])
        with self.assertRaisesRegex(app.ExportError, "pagination incomplete"):
            client.get_all("/list", max_pages=2)
        for response in [{}, {"records": [{}]}, {"records": [], "paging": {"totalPages": "bad"}}]:
            client.get = lambda p, q, response=response: response
            with self.assertRaises(app.ExportError):
                client.get_all("/list")

    def test_invalid_json_and_bad_login(self):
        response = FakeResponse({})
        response.data = b"not JSON"
        with self.assertRaisesRegex(app.ExportError, "invalid JSON"):
            self.client(lambda *a, **k: response).get("/list")
        os.environ.update(RC_CLIENT_ID="client", RC_CLIENT_SECRET="secret", RC_JWT="jwt")
        with self.assertRaisesRegex(app.ExportError, "no access token"):
            self.client(lambda *a, **k: FakeResponse({})).login()

    def test_server_origin_validation(self):
        for server in ["http://example.invalid", "https://user:secret@example.invalid", "https://example.invalid/path", "https://example.invalid?x=1"]:
            with self.assertRaises(app.ExportError):
                app.RcClient(server, QuietLog())


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rc-python-tests-")
        self.addCleanup(self.temp.cleanup)
        self.options = opts("--site-id", "101", "--no-open", "--out", self.temp.name)

    def result(self, overrides=None):
        return app.run_export(self.options, FixtureClient(overrides))

    def test_account_zone_site_override_and_unmodified_raw_schedule(self):
        regional_path = "/restapi/v2/accounts/~/regional-settings"
        settings = {"timezone": {"id": "east", "name": "America/New_York"}}
        directory, manifest = self.result({
            regional_path: settings,
            f"{BASE}/sites/101": {"regionalSettings": {"timezone": {"id": "london", "name": "Europe/London"}}},
            f"{BASE}/extension/101": {"regionalSettings": {"timezone": {"name": "UTC"}}},
        })
        self.assertEqual(manifest["account"]["regionalSettings"], settings)
        self.assertEqual(manifest["account"]["timeZone"], {"value": settings["timezone"], "source": "Account regional settings"})
        site = json.loads((directory / "sites.json").read_text())[0]
        self.assertEqual(site["timeZone"]["value"]["name"], "Europe/London")
        self.assertEqual(site["timeZone"]["source"], "detail")
        self.assertEqual(site["customRules"][0], FULL_RULE)
        with (directory / "site-custom-rules.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["timeZone"], "Europe/London")
        self.assertEqual(row["timeZoneSource"], "detail")
        self.assertEqual(row["record.schedule.weeklyRanges.monday[0].from"], "09:00")
        index = json.loads((directory / "raw" / "responses" / "index.json").read_text())
        response = next(r for r in index if r["path"] == regional_path)
        self.assertEqual(json.loads((directory / "raw" / "responses" / response["file"]).read_text()), settings)

    def test_site_zone_uses_extension_then_discovery_then_explicit_account_fallback(self):
        for source in ("extension", "list", "fallback"):
            with self.subTest(source=source):
                overrides = {f"{BASE}/sites/101": {}, f"{BASE}/extension/101": {}}
                if source == "extension":
                    overrides[f"{BASE}/extension/101"] = {"regionalSettings": {"timezone": {"name": "Europe/London"}}}
                if source == "list":
                    records = copy.deepcopy(SITES)
                    records[1]["regionalSettings"] = {"timezone": {"name": "Europe/London"}}
                    overrides[f"{BASE}/sites"] = lambda query: paginate(records, query)
                directory, _ = self.result(overrides)
                zone = json.loads((directory / "sites.json").read_text())[0]["timeZone"]
                self.assertEqual(zone["source"], source if source != "fallback" else "Account fallback (site time zone not returned)")
                self.assertEqual(zone["value"]["name"], "UTC" if source == "fallback" else "Europe/London")

    def test_account_zone_falls_back_to_main_site_even_when_not_selected(self):
        path = "/restapi/v2/accounts/~/regional-settings"
        records = copy.deepcopy(SITES)
        records[0]["regionalSettings"] = {"timezone": {"name": "America/Chicago"}}
        directory, manifest = self.result({path: app.RcError(403, {}, path),
                                           f"{BASE}/sites": lambda query: paginate(records, query)})
        self.assertEqual(manifest["account"]["timeZone"]["source"], "Main Site discovery")
        self.assertEqual(manifest["account"]["timeZone"]["value"]["name"], "America/Chicago")
        self.assertEqual(manifest["account"]["regionalSettingsStatus"]["status"], "incomplete")
        self.assertTrue(any(e["label"] == "account regional settings" for e in manifest["errors"]))
        self.assertEqual(len(json.loads((directory / "sites.json").read_text())), 1)

    def test_unknown_account_zone_is_not_inferred_from_selected_site(self):
        path = "/restapi/v2/accounts/~/regional-settings"
        directory, manifest = self.result({
            path: app.RcError(404, {}, path),
            f"{BASE}/sites/101": {"regionalSettings": {"timezone": {"name": "Europe/London"}}},
        })
        self.assertIsNone(manifest["account"]["timeZone"]["value"])
        self.assertTrue(any("Account time zone was not returned" in n for n in manifest["notes"]))
        self.assertIn("Account time zone: Not returned", (directory / "SUMMARY.md").read_text())
        self.assertEqual(json.loads((directory / "sites.json").read_text())[0]["timeZone"]["value"]["name"], "Europe/London")

    def test_account_info_timezone_fallback_and_missing_site_zone(self):
        path = "/restapi/v2/accounts/~/regional-settings"
        _, manifest = self.result({path: app.RcError(403, {}, path),
                                   BASE: {"id": 1, "regionalSettings": {"timezone": {"name": "UTC"}}}})
        self.assertEqual(manifest["account"]["timeZone"]["source"], "Account info")
        self.assertEqual(app.configured_timezone(("Empty", {"timezone": {}})), {"value": None, "source": "Not returned"})
        self.assertEqual(app.timezone_label({"value": {"id": "58"}}), "Time zone ID 58")

    def test_missing_account_and_site_zones_remain_unknown(self):
        path = "/restapi/v2/accounts/~/regional-settings"
        client = FixtureClient({path: {}, f"{BASE}/sites/101": {}, f"{BASE}/extension/101": {}})
        directory, manifest = app.run_export(self.options, client)
        self.assertIsNone(manifest["account"]["timeZone"]["value"])
        site = json.loads((directory / "sites.json").read_text())[0]
        self.assertEqual(site["timeZone"], {"value": None, "source": "Not returned"})
        self.assertNotIn(f"{BASE}/extension/~", [p for p, _ in client.calls])

    def test_outputs_full_fields_raw_and_no_queues(self):
        directory, manifest = self.result()
        data = json.loads((directory / "sites.json").read_text(encoding="utf-8"))
        self.assertEqual(data[0]["customRules"][0], FULL_RULE)
        with (directory / "site-custom-rules.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertNotIn("details", rows[0])
        self.assertEqual(rows[0]["record.forwarding.rules[0].forwardingNumbers[0].phoneNumber"], "+15550000003")
        self.assertEqual(rows[0]["record.additionalField.nested[1].preserve"], "everything")
        self.assertEqual(rows[0]["record.schedule.weeklyRanges.monday[0].from"], "09:00")
        self.assertEqual(rows[1]["enabled"], "false")
        self.assertEqual(rows[0]["detailStatus"], "complete")
        for path in ["report.html", "SUMMARY.md", "export-manifest.json", "site-answering-rules.csv",
                     "site-members.csv", "site-phone-numbers.csv", "site-ivr-menus.csv", "emergency-locations.csv"]:
            self.assertTrue((directory / path).is_file(), path)
        self.assertEqual(manifest["counts"]["answeringRules"], 4)
        self.assertEqual(manifest["counts"]["customRules"], 2)
        self.assertTrue(all("queue" not in name for name in manifest["files"]))
        index = json.loads((directory / "raw" / "responses" / "index.json").read_text())
        self.assertTrue(all(r["path"] != "/restapi/oauth/token" for r in index))
        record = next(r for r in index if r["path"].endswith("/answering-rule/custom%2F1"))
        self.assertEqual(json.loads((directory / "raw" / "responses" / record["file"]).read_text()), FULL_RULE)

    def test_empty_csv_headers_and_unique_output_folders(self):
        path = f"{BASE}/extension/101/answering-rule"
        first, _ = self.result({path: {"records": []}})
        second, _ = self.result({path: {"records": []}})
        self.assertNotEqual(first, second)
        with (first / "site-custom-rules.csv").open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertIn("detailStatus", reader.fieldnames)
            self.assertEqual(list(reader), [])

    def test_incomplete_counts_blank_csv_and_report_warning(self):
        path = f"{BASE}/extension/101/answering-rule"
        directory, manifest = self.result({path: app.RcError(403, {}, path)})
        self.assertEqual(manifest["counts"]["incompleteRuleListings"], 1)
        with (directory / "sites.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["ruleCount"], "")
        report = (directory / "report.html").read_text()
        self.assertIn("unknown, not zero", report)
        self.assertIn("Export is incomplete", report)
        self.assertIsNone(report_payload(report)["sites"][0]["answeringRules"])

    def test_report_has_readable_fields_and_safe_markup(self):
        directory, _ = self.result()
        report = (directory / "report.html").read_text()
        for content in ["Caller conditions", "Greetings", "IVR menus", "Emergency locations",
                        "Business hours", "Details: ", "Disabled", "Download JSON"]:
            self.assertIn(content, report)
        self.assertNotIn("Call queue:", report)
        self.assertNotIn("https://cdn.", report)
        self.assertEqual(app.escape('<script>"x"</script>'), "&lt;script&gt;&quot;x&quot;&lt;/script&gt;")
        self.assertEqual(report_payload(report)["sites"][0]["answeringRules"][1], FULL_RULE)

    def test_application_name_in_report_summary_and_cli(self):
        name = "RingCentral - Export Site Configurations"
        directory, _ = self.result()
        report = (directory / "report.html").read_text()
        self.assertIn(f"<title>{name}</title>", report)
        self.assertIn(f"<h1>{name}</h1>", report)
        self.assertIn(f"document.title = '{name} - '", report)
        self.assertTrue((directory / "SUMMARY.md").read_text().startswith(f"# {name}\n"))
        with patch("sys.stdout", new=StringIO()) as output, self.assertRaises(SystemExit):
            opts("--help")
        self.assertIn(name, output.getvalue())

    def test_browser_open_and_no_open(self):
        directory = Path(self.temp.name)
        manifest = {"counts": {}, "errors": []}
        with patch.object(app, "run_export", return_value=(directory, manifest)), patch.object(app.webbrowser, "open", return_value=True) as browser, patch("sys.stdout", new=StringIO()):
            self.assertEqual(app.main(["--all-sites", "--no-open"]), 0)
            browser.assert_not_called()
            self.assertEqual(app.main(["--all-sites"]), 0)
            browser.assert_called_once_with((directory / "report.html").as_uri())
        with patch.object(app, "run_export", return_value=(directory, manifest)), patch.object(app.webbrowser, "open", return_value=False), patch("sys.stderr", new=StringIO()) as errors, patch("sys.stdout", new=StringIO()):
            self.assertEqual(app.main(["--all-sites"]), 0)
            self.assertIn("WARN", errors.getvalue())

    def test_env_file_existing_variables_win(self):
        env = Path(self.temp.name) / "sample.env"
        env.write_text('RC_CLIENT_ID=file-id\nRC_JWT="example-jwt"\nexport RC_SERVER_URL=https://example.invalid # note\n', encoding="utf-8")
        with patch.dict(os.environ, {"RC_CLIENT_ID": "existing"}, clear=True):
            self.assertEqual(app.load_env_file(env), 2)
            self.assertEqual(os.environ["RC_CLIENT_ID"], "existing")
            self.assertEqual(os.environ["RC_JWT"], "example-jwt")

    def test_report_escapes_site_rule_note_and_error_content(self):
        sites, ctx = export(FixtureClient(), "--site-id", "101")
        payload = '</title><script>alert("test")</script><img src=x onerror="alert(1)">'
        sites[0]["name"] = payload
        sites[0]["answeringRules"][1]["name"] = payload
        result = {"account": {"id": "test", "name": payload}, "finishedAt": payload, "sites": sites,
                  "counts": {}, "notes": [payload], "errors": [{"label": payload, "message": payload}]}
        report = app.render_report(result, ["sites.json"])
        self.assertNotIn(payload, report)
        self.assertEqual(report_payload(report)["account"]["name"], payload)
        self.assertEqual(report_payload(report)["sites"][0]["answeringRules"][1]["name"], payload)
        self.assertEqual(report.count("<script>"), 1)

    def test_flattened_csv_keeps_every_nested_value_and_array_position(self):
        source = {"name": "Sample", "enabled": False, "nested": {"items": [{"id": "a", "time": 0}, {"id": "b", "flag": True}]},
                  "emptyList": [], "emptyObject": {}, "unset": None}
        columns = app.flatten_columns(source, "record")
        self.assertEqual(columns["record.nested.items[0].time"], 0)
        self.assertEqual(columns["record.nested.items[1].id"], "b")
        self.assertEqual(columns["record.nested.items[1].flag"], True)
        self.assertEqual(columns["record.emptyList"], [])
        self.assertEqual(columns["record.emptyObject"], {})
        self.assertIsNone(columns["record.unset"])
        path = Path(self.temp.name) / "flattened.csv"
        app.write_csv(path, [columns, {"record.newField": "only second record"}], ["record.name"])
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["record.enabled"], "false")
        self.assertEqual(rows[0]["record.nested.items[0].time"], "0")
        self.assertEqual(rows[1]["record.newField"], "only second record")

    def test_phone_and_site_csv_have_compartmentalized_columns(self):
        directory, _ = self.result()
        with (directory / "site-phone-numbers.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["record.phoneNumber"], "+15550000001")
        self.assertEqual(rows[1]["record.site.id"], "101")
        self.assertNotIn("details", rows[0])
        with (directory / "sites.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertIn("site.detail.settings.regionalSettings.timezone.name", row)
        self.assertFalse(any("answeringRules[" in key for key in row))

    def test_large_report_keeps_data_without_pre_rendering_thousands_of_cards(self):
        result = large_report_result()
        report = app.render_report(result, ["sites.json"])
        payload = report_payload(report)
        self.assertEqual(sum(len(s["answeringRules"]) for s in payload["sites"]), 1450)
        self.assertEqual(sum(r["type"] == "Custom" for s in payload["sites"] for r in s["answeringRules"]), 1305)
        self.assertTrue(all("customRules" not in s for s in payload["sites"]))
        self.assertIn('value="10"', report)
        self.assertIn('value="50" selected', report)
        self.assertIn('value="100"', report)
        self.assertIn('value="500"', report)
        self.assertIn('value="all"', report)
        self.assertNotIn("<article", report)
        self.assertNotIn("<td>Rule 1449", report)
        self.assertEqual(payload["sites"][1]["answeringRules"][-1]["id"], "rule-1449")

    def test_report_has_column_resize_controls_and_per_day_schedule_layout(self):
        result = large_report_result(20)
        rule = result["sites"][0]["answeringRules"][0]
        rule["schedule"]["weeklyRanges"]["sunday"] = [
            {"from": "00:00", "to": "07:00"}, {"from": "18:00", "to": "23:59"}
        ]
        report = app.render_report(result, [])
        for markup in ['id="column-widths"', 'id="reset-widths"', 'class="column-resizer"',
                       "setPointerCapture", "pointercancel", "lostpointercapture", "ArrowLeft",
                       "ArrowRight", 'class="schedule-day"', "scheduleCell(r.schedule,item.site.timeZone)",
                       "#records{table-layout:fixed;min-width:100%}"]:
            self.assertIn(markup, report)
        self.assertNotIn("-webkit-line-clamp", report)
        self.assertEqual(report_payload(report)["sites"][0]["answeringRules"][0], rule)

    def test_large_custom_rule_csv_has_scalar_columns_for_all_1305_rules(self):
        rows = app.csv_rows(large_report_result()["sites"], "customRules")
        self.assertEqual(len(rows), 1305)
        path = Path(self.temp.name) / "large-custom-rules.csv"
        app.write_csv(path, rows, ["siteId", "siteName", "id", "name", "enabled", "detailStatus", "detailError"])
        with path.open(encoding="utf-8-sig", newline="") as handle:
            saved = list(csv.DictReader(handle))
        self.assertEqual(len(saved), 1305)
        self.assertEqual(saved[-1]["id"], "rule-1449")
        self.assertEqual(saved[-1]["record.transfer.extension.id"], "target-1449")
        self.assertEqual(saved[-1]["record.schedule.weeklyRanges.monday[0].to"], "17:00")
        self.assertTrue(any(row["enabled"] == "false" for row in saved))
        self.assertTrue(any(row["detailStatus"] == "incomplete" and row["detailError"] for row in saved))
        self.assertNotIn("details", saved[0])


if __name__ == "__main__":
    unittest.main()
