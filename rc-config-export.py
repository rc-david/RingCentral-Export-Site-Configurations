#!/usr/bin/env python3
"""RingCentral - Export Site Configurations. Read-only; Python 3.10+, standard library only."""

import argparse
import base64
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import webbrowser


BASE = "/restapi/v1.0/account/~"
SERVER = "https://platform.ringcentral.com"


class ExportError(Exception):
    """An API or configuration failure that can be reported without exposing secrets."""


class RcError(ExportError):
    def __init__(self, status, body, path, page=None):
        errors = body.get("errors") or [{}]
        self.status = status
        self.code = body.get("errorCode") or errors[0].get("errorCode") or body.get("error") or ""
        self.message = body.get("message") or errors[0].get("message") or body.get("error_description") or f"HTTP {status}"
        self.path = path
        self.page = page
        super().__init__(f"{status} {self.code} {self.message}".strip())


class Logger:
    def __init__(self, quiet=False):
        self.quiet = quiet

    def info(self, message):
        if not self.quiet:
            print(f"{datetime.now(timezone.utc):%H:%M:%S}  {message}", file=sys.stderr)

    def warn(self, message):
        print(f"{datetime.now(timezone.utc):%H:%M:%S}  WARN {message}", file=sys.stderr)


def load_env_file(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ExportError(f"cannot read env file {path}: {error}") from error
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = re.sub(r"^export\s+", "", key.strip())
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="RingCentral - Export Site Configurations. Export sites to offline HTML, JSON and CSV. No account settings are changed.")
    parser.add_argument("--env-file", type=Path, help="load RC_* variables; existing environment variables win")
    parser.add_argument("--out", type=Path, default=Path("exports"), help="output root (default: exports)")
    parser.add_argument("--server", help="API server (default: RC_SERVER_URL or production)")
    parser.add_argument("--all-sites", action="store_true", help="explicitly export all sites")
    parser.add_argument("--site-id", dest="site_ids", action="append", default=[], help="exact site ID; repeat as needed")
    parser.add_argument("--site-name", dest="site_names", action="append", default=[], help="exact, case-sensitive site name; repeat as needed")
    parser.add_argument("--ring-group-details", action="store_true",
                        help="also read each custom-rule target extension's devices, phone-line numbers, serial/MAC addresses and ring settings (extra API calls per target)")
    parser.add_argument("--no-open", dest="open", action="store_false", help="save report.html without opening a browser")
    parser.add_argument("--quiet", action="store_true", help="suppress progress; warnings and final summary still print")
    args = parser.parse_args(argv)
    try:
        validate_selection(args)
    except ExportError as error:
        parser.error(str(error))
    return args


def validate_selection(opts):
    if any(not isinstance(v, str) or not v.strip() for v in opts.site_ids + opts.site_names):
        raise ExportError("site IDs and names must be non-empty strings")
    selected = bool(opts.site_ids or opts.site_names)
    if opts.all_sites == selected:
        raise ExportError("choose --all-sites OR at least one --site-id / --site-name, not both")


def select_sites(records, opts):
    validate_selection(opts)
    if opts.all_sites:
        return records
    selected = set()
    for site_id in opts.site_ids:
        if not any(str(s.get("id")) == site_id for s in records):
            raise ExportError(f'unknown site ID "{site_id}"')
        selected.add(site_id)
    for name in opts.site_names:
        matches = [s for s in records if s.get("name") == name]
        if not matches:
            raise ExportError(f'unknown site name "{name}" (exact, case-sensitive names required)')
        if len(matches) > 1:
            raise ExportError(f'ambiguous site name "{name}"; use --site-id ({", ".join(str(s["id"]) for s in matches)})')
        selected.add(str(matches[0]["id"]))
    return [s for s in records if str(s["id"]) in selected]


def numeric_header(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


class RcClient:
    def __init__(self, server, log, *, opener=urlopen, sleep=time.sleep, clock=time.monotonic):
        parsed = urlsplit(server)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ExportError("RC_SERVER_URL / --server must be an HTTPS server origin without credentials, query or path")
        self.server = server.rstrip("/")
        self.log = log
        self.opener, self.sleep, self.clock = opener, sleep, clock
        self.token = os.environ.get("RC_ACCESS_TOKEN")
        self.jwt = os.environ.get("RC_JWT")
        self.timeout = 45
        self.requests = self.retries = 0
        self.responses = []
        self.groups = {}
        self.pattern_groups = {}

    def request(self, method, path, query=None, body=None, headers=None):
        url = self.server + path
        if query:
            url += "?" + urlencode(query)
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                status, response_headers, raw = response.status, response.headers, response.read()
        except HTTPError as error:
            with error:
                status, response_headers, raw = error.code, error.headers, error.read()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            if status >= 400:
                data = {"message": f"HTTP {status}: non-JSON response"}
            else:
                raise ExportError(f"invalid JSON response for {path}") from error
        if not isinstance(data, dict):
            raise ExportError(f"invalid response for {path}: expected an object")
        return status, response_headers, data

    def login(self, force=False):
        if self.token and not force:
            self.log.info("using RC_ACCESS_TOKEN; skipping JWT login")
            return
        client_id, secret = os.environ.get("RC_CLIENT_ID"), os.environ.get("RC_CLIENT_SECRET")
        if not client_id or not secret or not self.jwt:
            raise ExportError("RC_CLIENT_ID, RC_CLIENT_SECRET and RC_JWT (or RC_ACCESS_TOKEN) are required")
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        try:
            status, _, data = self.request(
                "POST", "/restapi/oauth/token",
                body=urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self.jwt}).encode(),
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            )
        except (URLError, OSError, http.client.HTTPException) as error:
            raise ExportError("JWT login network failure; check connectivity and API server") from error
        if status >= 400:
            raise RcError(status, data, "/restapi/oauth/token")
        if not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise ExportError("JWT login returned no access token")
        self.token = data["access_token"]
        self.log.info(f'logged in as extension {data.get("owner_id", "?")} (token valid {data.get("expires_in", "?")}s)')

    def get(self, path, query=None):
        pattern = re.sub(r"/\d+(?=/|$)", "/{id}", path)
        refreshed = False
        for attempt in range(6):
            group = self.pattern_groups.get(pattern)
            if group in self.groups:
                remaining, reset = self.groups[group]
                if remaining <= 0 and reset > self.clock():
                    wait = reset - self.clock() + 0.25
                    self.log.info(f"rate limit group {group} exhausted; waiting {wait:.1f}s")
                    self.sleep(wait)
            self.requests += 1
            try:
                status, headers, data = self.request("GET", path, query, headers={
                    "Authorization": f"Bearer {self.token}", "Accept": "application/json",
                })
            except (URLError, OSError, http.client.HTTPException) as error:
                if attempt >= 3:
                    raise ExportError(f"network error on GET {path}; check connectivity") from error
                self.retries += 1
                self.log.warn(f"network failure on {path}; retrying")
                self.sleep(2 ** attempt)
                continue
            group = headers.get("X-Rate-Limit-Group")
            remaining = numeric_header(headers.get("X-Rate-Limit-Remaining"))
            window = numeric_header(headers.get("X-Rate-Limit-Window")) or 60
            if group and remaining is not None:
                self.pattern_groups[pattern] = group
                self.groups[group] = (remaining, self.clock() + window)
            if status == 401 and not refreshed and self.jwt and attempt < 5:
                self.log.warn("access token rejected; authenticating with JWT again")
                self.login(force=True)
                refreshed = True
                self.retries += 1
                continue
            if status == 429 and attempt < 5:
                wait = numeric_header(headers.get("Retry-After"))
                if wait is None:
                    try:
                        wait = max(0, parsedate_to_datetime(headers.get("Retry-After", "")).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        wait = window
                self.log.warn(f"429 on {path}; retrying in {wait:.1f}s")
                self.retries += 1
                self.sleep(wait + 0.25)
                continue
            if status >= 500 and attempt < 3:
                self.log.warn(f"{status} on {path}; retrying")
                self.retries += 1
                self.sleep(1.5 * 2 ** attempt)
                continue
            if status >= 400:
                raise RcError(status, data, path)
            self.responses.append({"path": path, "query": dict(query or {}), "data": data})
            return data
        raise ExportError(f"retry limit exhausted for {path}")

    def get_all(self, path, query=None, max_pages=200):
        records = []
        for page in range(1, max_pages + 1):
            try:
                data = self.get(path, {**(query or {}), "page": page, "perPage": 1000})
            except RcError as error:
                error.page = page
                raise
            items = data.get("records")
            if not isinstance(items, list) or any(not isinstance(item, dict) or item.get("id") is None for item in items):
                raise ExportError(f"invalid list response for {path}: expected records with IDs")
            records.extend(items)
            total = data.get("paging", {}).get("totalPages")
            if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
                raise ExportError(f"invalid pagination response for {path}: totalPages must be a non-negative integer")
            has_next = page < total if total else bool(data.get("navigation", {}).get("nextPage"))
            if not has_next:
                return records
        raise ExportError(f"pagination incomplete for {path}: more than {max_pages} pages")


def error_record(label, error):
    return {"label": label, "status": getattr(error, "status", None),
            "errorCode": getattr(error, "code", None), "message": str(error),
            "path": getattr(error, "path", None)}


def attempt(ctx, label, operation):
    try:
        return operation(), None
    except (ExportError, OSError, http.client.HTTPException) as error:
        record = error_record(label, error)
        ctx["errors"].append(record)
        ctx["log"].warn(f'{label}: {record["message"]}')
        return None, record


def read_field(rc, out, local, field, path, *, listing=False, query=None):
    data, error = attempt(local, f'{out["name"]} {field}', lambda: rc.get_all(path, query) if listing else rc.get(path, query))
    out[field] = data
    out["dataStatus"][field] = {"status": "incomplete" if error else "complete", "error": error}
    return data


def fetch_details(rc, local, label, path, records):
    full, statuses = [], []
    for entry in records or []:
        item_id = entry.get("id")

        def read():
            if item_id is None or str(item_id) == "":
                raise ExportError(f"{label} listing is missing an ID")
            data = rc.get(f"{path}/{quote(str(item_id), safe='')}")
            if not isinstance(data, dict) or str(data.get("id")) != str(item_id):
                raise ExportError(f"invalid detail response for {label} {item_id}")
            return data

        data, error = attempt(local, f'{label} "{entry.get("name", item_id)}"', read)
        full.append(data if data is not None else entry)
        statuses.append({"id": item_id, "status": "incomplete" if error else "complete", "error": error})
    return full, statuses


def grouped(records):
    groups = {}
    for record in records or []:
        key = str((record.get("site") or {}).get("id") or "main-site")
        groups.setdefault(key, []).append(record)
    return groups


def collection_status(listing_error, details):
    error = listing_error or next((d["error"] for d in details if d["status"] != "complete"), None)
    return {"status": "incomplete" if error else "complete", "error": error}


def configured_timezone(*sources):
    for source, settings in sources:
        zone = (settings or {}).get("timezone")
        if isinstance(zone, dict) and any(zone.get(key) for key in ("name", "description", "id")):
            return {"value": zone, "source": source}
    return {"value": None, "source": "Not returned"}


def timezone_label(zone):
    value = (zone or {}).get("value") or {}
    return value.get("name") or value.get("description") or (
        f'Time zone ID {value["id"]}' if value.get("id") else "Not returned"
    )


def export_sites(rc, opts, ctx):
    ctx["log"].info("listing sites")
    synthetic = False
    try:
        records = rc.get_all(f"{BASE}/sites")
    except RcError as error:
        if error.page != 1 or error.status not in (400, 403, 404):
            raise
        synthetic = True
        record = error_record("site discovery", error)
        ctx["errors"].append(record)
        ctx["log"].warn(f"site discovery unavailable: {error}; falling back to company-level Main Site only")
        ctx["notes"].append("Site discovery was unavailable. Company-level Main Site is a fallback; this does not prove multi-site is disabled or that all sites were exported.")
        records = [{"id": "main-site", "name": "Main Site"}]
    account_info = ctx.get("account")
    if account_info is not None and not account_info["timeZone"]["value"]:
        main_entry = next((record for record in records if str(record["id"]) == "main-site"), {})
        account_info["timeZone"] = configured_timezone(("Main Site discovery", main_entry.get("regionalSettings")))
    selected = select_sites(records, opts)
    ctx["log"].info(f"selected {len(selected)} site(s)")
    if not selected:
        return []
    menus, menu_error = attempt(ctx, "IVR menu listing", lambda: rc.get_all(f"{BASE}/ivr-menus"))
    emergency, emergency_error = attempt(ctx, "emergency-location listing", lambda: rc.get_all(f"{BASE}/emergency-locations"))
    menu_groups, emergency_groups = grouped(menus), grouped(emergency)
    all_members = None
    member_error = None
    if any(str(s["id"]) == "main-site" for s in selected):
        all_members, member_error = attempt(ctx, "Main Site member discovery", lambda: rc.get_all(f"{BASE}/extension"))
    sites = []
    for entry in selected:
        site_id = str(entry["id"])
        main = site_id == "main-site"
        out = {"id": site_id, "name": entry.get("name") or site_id, "extensionNumber": entry.get("extensionNumber"),
               "list": entry, "mainSite": main, "synthetic": synthetic, "errors": [], "dataStatus": {}}
        local = {"errors": out["errors"], "log": ctx["log"]}
        encoded = quote(site_id, safe="")
        extension_path = BASE if main else f"{BASE}/extension/{encoded}"
        if main:
            out["detail"] = entry
            read_field(rc, out, local, "account", BASE)
            out["members"] = all_members if synthetic or all_members is None else [
                m for m in all_members if str((m.get("site") or {}).get("id") or "main-site") == "main-site"
            ]
            out["dataStatus"]["members"] = {"status": "incomplete" if member_error else "complete", "error": member_error}
            read_field(rc, out, local, "ivr", f"{BASE}/ivr")
        else:
            read_field(rc, out, local, "detail", f"{BASE}/sites/{encoded}")
            read_field(rc, out, local, "extension", extension_path)
            read_field(rc, out, local, "ivr", f"{BASE}/sites/{encoded}/ivr")
            read_field(rc, out, local, "members", f"{BASE}/sites/{encoded}/members", listing=True)
        read_field(rc, out, local, "businessHours", f"{extension_path}/business-hours")
        numbers = read_field(rc, out, local, "phoneNumbers", f"{extension_path}/phone-number", listing=True)
        if main and not synthetic and numbers is not None:
            out["phoneNumbers"] = grouped(numbers).get("main-site", [])
        listing = read_field(rc, out, local, "answeringRuleListing", f"{extension_path}/answering-rule", listing=True, query={"view": "Detailed"})
        rules, statuses = fetch_details(rc, local, f'{out["name"]} answering rule', f"{extension_path}/answering-rule", listing)
        out["answeringRules"] = rules if listing is not None else None
        out["answeringRuleDetails"] = statuses
        out["dataStatus"]["answeringRules"] = collection_status(out["dataStatus"]["answeringRuleListing"]["error"], statuses)
        out["customRuleListingStatus"] = "complete" if listing is not None else "incomplete"
        custom_indices = [i for i, r in enumerate(listing or []) if r.get("type") == "Custom"]
        out["customRules"] = [rules[i] for i in custom_indices] if listing is not None else None
        out["customRuleDetails"] = [{"ruleId": statuses[i]["id"], **statuses[i]} for i in custom_indices]
        menu_list = (menus or []) if synthetic else menu_groups.get(site_id, [])
        menu_list = list(menu_list)
        top_menu = (out.get("ivr") or {}).get("topMenu") or {}
        if top_menu.get("id") is not None and not any(str(m["id"]) == str(top_menu["id"]) for m in menu_list):
            menu_list.append(top_menu)
        out["ivrMenuListing"] = menu_list if menus is not None else None
        out["ivrMenus"], out["ivrMenuDetails"] = fetch_details(rc, local, f'{out["name"]} IVR menu', f"{BASE}/ivr-menus", menu_list)
        if menus is None and not menu_list:
            out["ivrMenus"] = None
        out["dataStatus"]["ivrMenus"] = collection_status(menu_error, out["ivrMenuDetails"])
        locations = (emergency or []) if synthetic else emergency_groups.get(site_id, [])
        out["emergencyLocationListing"] = locations if emergency is not None else None
        full_locations, location_statuses = fetch_details(rc, local, f'{out["name"]} emergency location', f"{BASE}/emergency-locations", locations)
        out["emergencyLocations"] = full_locations if emergency is not None else None
        out["emergencyLocationDetails"] = location_statuses
        out["dataStatus"]["emergencyLocations"] = collection_status(emergency_error, location_statuses)
        out["timeZone"] = configured_timezone(*[
            (source, (out.get(source) or {}).get("regionalSettings"))
            for source in ("detail", "extension", "list", "account")
        ])
        if not out["timeZone"]["value"] and account_info and account_info["timeZone"]["value"]:
            out["timeZone"] = {"value": account_info["timeZone"]["value"],
                               "source": "Account fallback (site time zone not returned)"}
        if listing is None:
            ctx["notes"].append(f'Site "{out["name"]}": rule listing unavailable; rule count is unknown, not zero.')
        ctx["errors"].extend(out["errors"])
        sites.append(out)
        ctx["log"].info(f'{out["name"]}: {len(rules)} discovered rule(s), {len(out["errors"])} unavailable endpoint(s)')
    return sites


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_")[:60] or "unnamed"


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten_columns(value, prefix=""):
    """Use indexed array paths so repeated records retain their association."""
    if isinstance(value, dict) and value:
        result = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            result.update(flatten_columns(item, path))
        return result
    if isinstance(value, list) and value:
        result = {}
        for index, item in enumerate(value):
            result.update(flatten_columns(item, f"{prefix}[{index}]"))
        return result
    return {prefix: value}


def csv_rows(sites, field):
    rows = []
    status_field = {"answeringRules": "answeringRuleDetails", "customRules": "customRuleDetails",
                    "ivrMenus": "ivrMenuDetails", "emergencyLocations": "emergencyLocationDetails"}.get(field)
    for site in sites:
        for index, record in enumerate(site.get(field) or []):
            detail_statuses = site.get(status_field, []) if status_field else []
            status = detail_statuses[index] if index < len(detail_statuses) else site["dataStatus"].get(field, {})
            rows.append({"siteId": site["id"], "siteName": site["name"], "order": index + 1,
                         "timeZone": timezone_label(site.get("timeZone")),
                         "timeZoneSource": (site.get("timeZone") or {}).get("source", "Not returned"),
                         "id": record.get("id"), "name": record.get("name"), "type": record.get("type"),
                         "enabled": record.get("enabled"), "detailStatus": status.get("status", "unknown"),
                         "detailError": (status.get("error") or {}).get("message", ""),
                         **flatten_columns(record, "record")})
    return rows


def write_csv(path, rows, columns):
    columns = list(dict.fromkeys([*columns, *(key for row in rows for key in row)]))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in columns})


def escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def label(value):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).replace("_", " ")


def render_report(result, files):
    report_sites = []
    routing = number_routing_rows(result["sites"], result.get("targetExtensions"))
    for site in result["sites"]:
        report_site = {key: value for key, value in site.items()
                       if key not in {"customRules", "customRuleDetails", "answeringRuleListing",
                                      "ivrMenuListing", "emergencyLocationListing"}}
        report_site["numberRouting"] = None if site["customRules"] is None else [
            {**row, "name": row["calledNumber"] or "(no called number condition)", "id": row["ruleId"]}
            for row in routing if row["siteId"] == site["id"]]
        if result.get("ringGroups") is not None:
            wanted = target_extension_ids([site])
            members = [m for s in result["sites"] for m in s.get("members") or []]
            report_site["ringGroupDetails"] = [
                {**row, "name": row["ringGroupName"] or row["ringGroupExtensionId"], "id": row["ringGroupExtensionId"]}
                for row in ring_group_rows([g for g in result["ringGroups"] if g["extensionId"] in wanted], routing,
                                           result.get("targetExtensions"), members)]
        report_site["dataStatus"] = {**site["dataStatus"], "numberRouting": site["dataStatus"].get("answeringRules", {"status": "unknown"})}
        report_sites.append(report_site)
    payload = {key: value for key, value in result.items() if key not in {"targetExtensions", "ringGroups"}}
    payload["sites"] = report_sites
    payload["extensionNames"] = extension_names(result["sites"], result.get("targetExtensions"))
    payload["ringGroupDetailsRequested"] = result.get("ringGroups") is not None
    # JSON lives in a non-executable script element; escape HTML delimiters, not JSON quotes.
    encoded = json.dumps(payload, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = Path(__file__).with_name("report-template.html").read_text(encoding="utf-8")
    links = "".join(f'<a href="{escape(f)}" download>{escape(f)}</a>' for f in files if "/" not in f)
    return template.replace("<!-- DOWNLOAD_LINKS -->", links).replace("<!-- REPORT_DATA -->", encoded)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


ROUTING_COLUMNS = ["siteId", "siteName", "ruleOrder", "ruleId", "ruleName", "enabled", "calledNumber", "callHandlingAction",
                   "targetExtensionId", "targetExtensionNumber", "targetName", "targetType", "forwardToNumbers", "targetStatus"]


def rule_targets(rule):
    """Return (extension ID or None, forwarding numbers) for where a rule sends calls."""
    extension_id = ((rule.get("transfer") or {}).get("extension") or {}).get("id")
    numbers = []
    unconditional = (rule.get("unconditionalForwarding") or {}).get("phoneNumber")
    if isinstance(unconditional, str):
        numbers.append(unconditional)
    for forwarding_rule in (rule.get("forwarding") or {}).get("rules") or []:
        numbers.extend(n.get("phoneNumber") for n in forwarding_rule.get("forwardingNumbers") or [] if n.get("phoneNumber"))
    return (None if extension_id is None else str(extension_id)), [n for n in numbers if n]


def unresolved_target_ids(sites):
    known = {str(m.get("id")) for site in sites for m in site.get("members") or []}
    wanted = {rule_targets(rule)[0] for site in sites for field in ("answeringRules", "customRules") for rule in site.get(field) or []}
    return {i for i in wanted if i and i not in known}


def extension_names(sites, extra_extensions=None):
    """Name, extension number and type for every extension a rule transfers to, so the report can show names instead of IDs."""
    lookup = {str(e.get("id")): e for e in extra_extensions or []}
    lookup.update({str(m.get("id")): m for site in sites for m in site.get("members") or []})
    wanted = {rule_targets(rule)[0] for site in sites for field in ("answeringRules", "customRules") for rule in site.get(field) or []}
    wanted |= {str(((rule.get("voicemail") or {}).get("recipient") or {}).get("id")) for site in sites
               for rule in site.get("answeringRules") or [] if ((rule.get("voicemail") or {}).get("recipient") or {}).get("id")}
    return {i: {key: lookup[i].get(key) for key in ("name", "extensionNumber", "type")} for i in wanted if i in lookup}


RING_GROUP_COLUMNS = ["ringGroupExtensionId", "ringGroupName", "ringGroupExtensionNumber", "ringGroupType", "usedByRules",
                      "ringingMode", "ringOrder", "ringDurationSeconds", "ringCount", "ringEntryEnabled", "memberKind",
                      "memberExtensionId", "memberName", "memberExtensionNumber", "memberExtensionType", "ringEntryNumber",
                      "deviceName", "deviceType", "deviceModel", "serialOrMac", "deviceStatus", "deviceDid", "deviceLineType",
                      "deviceId", "status"]
V2 = "/restapi/v2/accounts/~/extensions"


def target_extension_ids(sites):
    return {i for site in sites for rule in site.get("customRules") or [] for i in [rule_targets(rule)[0]] if i}


def ring_entries(rule, source):
    """Normalize ring settings from the new (v2 work-hours) or legacy (v1 business-hours) call handling API."""
    entries, mode = [], None
    if source == "v2":
        dispatching = (rule or {}).get("dispatching") or {}
        mode = dispatching.get("type")
        actions = [a for a in dispatching.get("actions") or [] if a.get("type") in ("RingGroupAction", "RingAlwaysGroupAction")]
        for order, action in enumerate(actions, 1):
            for target in action.get("targets") or []:
                kind = {"CoworkerRingTarget": "Co-worker", "AllDesktopRingTarget": "Desktop apps", "AllMobileRingTarget": "Mobile apps",
                        "PhoneNumberRingTarget": "External number", "DeviceRingTarget": "Device"}.get(target.get("type"), target.get("type"))
                entries.append({"order": order, "duration": action.get("duration"), "enabled": action.get("enabled"), "kind": kind,
                                "extensionId": None if not (target.get("extension") or {}).get("id") else str(target["extension"]["id"]),
                                "name": target.get("name"), "number": (target.get("destination") or {}).get("phoneNumber")})
    elif rule:
        forwarding = (rule or {}).get("forwarding") or {}
        mode = forwarding.get("ringingMode")
        for item in forwarding.get("rules") or []:
            for number in item.get("forwardingNumbers") or []:
                entries.append({"order": item.get("index"), "ringCount": item.get("ringCount"), "enabled": item.get("enabled"),
                                "kind": number.get("type") or "Device or number", "extensionId": None,
                                "name": number.get("label"), "number": number.get("phoneNumber")})
    return entries, mode


def read_ring_rule(rc, ctx, extension_id):
    """Prefer the new call handling API; accounts not yet migrated fall back to the legacy business-hours rule."""
    encoded = quote(extension_id, safe="")
    try:
        return rc.get(f"{V2}/{encoded}/comm-handling/voice/state-rules/work-hours"), "v2", None
    except (ExportError, OSError, http.client.HTTPException):
        rule, error = attempt(ctx, f"extension {extension_id} ring settings", lambda: rc.get(f"{BASE}/extension/{encoded}/answering-rule/business-hours-rule"))
        return rule, "v1", error


def read_extension_and_devices(rc, ctx, extension_id, with_devices=True):
    base = f"{BASE}/extension/{quote(extension_id, safe='')}"
    extension, e1 = attempt(ctx, f"extension {extension_id}", lambda: rc.get(base))
    devices, e2 = (None, None)
    if with_devices and (extension or {}).get("type") != "IvrMenu":
        devices, e2 = attempt(ctx, f"extension {extension_id} devices", lambda: rc.get_all(f"{base}/device"))
    return extension, devices, [e for e in (e1, e2) if e]


def fetch_ring_groups(rc, ctx, ids):
    """Read each target extension, its devices and ring settings, then each ring member's extension and devices (read-only GETs)."""
    groups, members = [], {}
    ordered = sorted(ids)
    for number, extension_id in enumerate(ordered, 1):
        ctx["log"].info(f"ring group details {number}/{len(ordered)}: extension {extension_id}")
        extension, devices, errors = read_extension_and_devices(rc, ctx, extension_id)
        rule, source, entries = None, None, []
        if (extension or {}).get("type") != "IvrMenu":
            rule, source, error = read_ring_rule(rc, ctx, extension_id)
            if error:
                errors.append(error)
            entries, _ = ring_entries(rule, source)
        groups.append({"extensionId": extension_id, "extension": extension, "devices": devices, "ringRule": rule, "ringRuleSource": source,
                       "memberDetails": members, "errors": errors})
    wanted = sorted({e["extensionId"] for g in groups for e in ring_entries(g["ringRule"], g["ringRuleSource"])[0] if e["extensionId"]})
    for number, member_id in enumerate(wanted, 1):
        ctx["log"].info(f"ring member details {number}/{len(wanted)}: extension {member_id}")
        extension, devices, errors = read_extension_and_devices(rc, ctx, member_id)
        members[member_id] = {"extension": extension, "devices": devices, "errors": errors}
    for group in groups:
        found = [e["message"] for e in group["errors"]]
        found += [m for e in ring_entries(group["ringRule"], group["ringRuleSource"])[0] if e["extensionId"]
                  for m in [x["message"] for x in members.get(e["extensionId"], {}).get("errors", [])]]
        group["status"] = "complete" if not found else "incomplete: " + "; ".join(dict.fromkeys(found))
    return groups


def digits(value):
    return re.sub(r"\D", "", str(value or ""))[-10:]  # ignore country code so "(480) 613-7524" matches "+14806137524"


def device_lines(devices):
    lines = []
    for device in devices or []:
        for line in device.get("phoneLines") or [{}]:
            lines.append({"deviceName": device.get("name") or device.get("computerName"), "deviceType": device.get("type"),
                          "deviceModel": (device.get("model") or {}).get("name") or device.get("sku"),
                          "serialOrMac": device.get("serial"), "deviceStatus": device.get("status"),
                          "deviceDid": (line.get("phoneInfo") or {}).get("phoneNumber"), "deviceLineType": line.get("lineType"),
                          "deviceId": device.get("id"), "used": False})
    return lines


def ring_group_rows(groups, routing, extra_extensions=None, members=None):
    """One row per ring entry (co-workers expanded to their devices and DIDs), then the ring group's own devices."""
    lookup = {str(e.get("id")): e for e in extra_extensions or []}
    lookup.update({str(m.get("id")): m for m in members or []})
    rows = []
    strip = lambda d: {k: v for k, v in d.items() if k != "used"}
    for group in groups:
        extension_id = group["extensionId"]
        extension = group["extension"] or lookup.get(extension_id) or {}
        used_by = " | ".join(dict.fromkeys(r["ruleName"] for r in routing if r["targetExtensionId"] == extension_id and r["ruleName"]))
        base = {"ringGroupExtensionId": extension_id, "ringGroupName": extension.get("name"),
                "ringGroupExtensionNumber": extension.get("extensionNumber"), "ringGroupType": extension.get("type"),
                "usedByRules": used_by, "status": group["status"]}
        own = device_lines(group["devices"])
        entries, mode = ring_entries(group["ringRule"], group.get("ringRuleSource"))
        base["ringingMode"] = mode
        for entry in entries:
            row = {**base, "ringOrder": entry.get("order"), "ringDurationSeconds": entry.get("duration"), "ringCount": entry.get("ringCount"),
                   "ringEntryEnabled": entry.get("enabled"), "memberKind": entry["kind"], "ringEntryNumber": entry.get("number"),
                   "memberName": entry.get("name")}
            if entry.get("extensionId"):
                detail = group["memberDetails"].get(entry["extensionId"]) or {}
                member = detail.get("extension") or lookup.get(entry["extensionId"]) or {}
                row.update({"memberExtensionId": entry["extensionId"], "memberName": member.get("name"),
                            "memberExtensionNumber": member.get("extensionNumber"), "memberExtensionType": member.get("type")})
                lines = device_lines(detail.get("devices"))
                rows.extend([{**row, **strip(line)} for line in lines] or [row])
                continue
            match = next((l for l in own if not l["used"] and digits(l["deviceDid"]) and digits(l["deviceDid"]) == digits(entry.get("number"))), None)
            if match:
                match["used"] = True
            rows.append({**row, **strip(match or {})})
        for line in own:
            if not line["used"]:
                rows.append({**base, "memberKind": "Ring group's own device", **strip(line)})
        if not entries and not own:
            rows.append(base)
    return rows


def number_routing_rows(sites, extra_extensions=None):
    """One row per custom rule and called number, with the target resolved to a name where known."""
    lookup = {str(e.get("id")): e for e in extra_extensions or []}
    for site in sites:
        lookup.update({str(m.get("id")): m for m in site.get("members") or []})
    rows = []
    for site in sites:
        for order, rule in enumerate(site.get("customRules") or [], 1):
            extension_id, forward_numbers = rule_targets(rule)
            target = lookup.get(extension_id) if extension_id else None
            called = [n.get("phoneNumber") for n in rule.get("calledNumbers") or [] if isinstance(n, dict)] or [""]
            status = "" if extension_id is None else ("resolved" if target else "extension not found in site members or account listing")
            for number in called:
                rows.append({"siteId": site["id"], "siteName": site["name"], "ruleOrder": order, "ruleId": rule.get("id"),
                             "ruleName": rule.get("name"), "enabled": rule.get("enabled"), "calledNumber": number,
                             "callHandlingAction": rule.get("callHandlingAction"), "targetExtensionId": extension_id,
                             "targetExtensionNumber": (target or {}).get("extensionNumber"), "targetName": (target or {}).get("name"),
                             "targetType": (target or {}).get("type"), "forwardToNumbers": " | ".join(forward_numbers),
                             "targetStatus": status})
    return rows


def write_outputs(directory, result, responses):
    directory.mkdir(parents=True, exist_ok=False)
    raw_sites = directory / "raw" / "sites"
    raw_responses = directory / "raw" / "responses"
    raw_sites.mkdir(parents=True)
    raw_responses.mkdir()
    files = ["sites.json", "sites.csv"]
    write_json(directory / "sites.json", result["sites"])
    site_rows = []
    for site in result["sites"]:
        write_json(raw_sites / f'{safe_name(site["id"])}_{safe_name(site["name"])}.json', site)
        site_rows.append({"id": site["id"], "name": site["name"], "extensionNumber": site["extensionNumber"],
                          "ruleCount": None if site["answeringRules"] is None else len(site["answeringRules"]),
                          "customRuleCount": None if site["customRules"] is None else len(site["customRules"]),
                          "customRuleListingStatus": site["customRuleListingStatus"],
                          "unavailableEndpoints": len(site["errors"]),
                          **flatten_columns({key: value for key, value in site.items()
                                             if key not in {"id", "name", "extensionNumber", "answeringRules", "customRules",
                                                            "answeringRuleListing", "customRuleDetails", "answeringRuleDetails",
                                                            "members", "phoneNumbers", "ivrMenus", "ivrMenuListing", "ivrMenuDetails",
                                                            "emergencyLocations", "emergencyLocationListing", "emergencyLocationDetails"}}, "site")})
    write_csv(directory / "sites.csv", site_rows,
              ["id", "name", "extensionNumber", "ruleCount", "customRuleCount", "customRuleListingStatus", "unavailableEndpoints"])
    for field, name in [("answeringRules", "site-answering-rules.csv"), ("customRules", "site-custom-rules.csv"),
                        ("members", "site-members.csv"), ("phoneNumbers", "site-phone-numbers.csv"),
                        ("ivrMenus", "site-ivr-menus.csv"), ("emergencyLocations", "emergency-locations.csv")]:
        files.append(name)
        write_csv(directory / name, csv_rows(result["sites"], field),
                  ["siteId", "siteName", "timeZone", "timeZoneSource", "order", "id", "name", "type", "enabled", "detailStatus", "detailError"])
    if result.get("ringGroups") is not None:
        files.append("ring-group-details.csv")
        members = [m for site in result["sites"] for m in site.get("members") or []]
        write_csv(directory / "ring-group-details.csv",
                  ring_group_rows(result["ringGroups"], number_routing_rows(result["sites"], result.get("targetExtensions")),
                                  result.get("targetExtensions"), members), RING_GROUP_COLUMNS)
    files.append("number-routing.csv")
    write_csv(directory / "number-routing.csv", number_routing_rows(result["sites"], result.get("targetExtensions")), ROUTING_COLUMNS)
    response_index = []
    for index, response in enumerate(responses, 1):
        name = f"{index:05d}.json"
        write_json(raw_responses / name, response["data"])
        response_index.append({"file": name, "path": response["path"], "query": response["query"]})
    write_json(raw_responses / "index.json", response_index)
    files.extend(["raw/sites/", "raw/responses/", "report.html", "SUMMARY.md", "export-manifest.json"])
    (directory / "report.html").write_text(render_report(result, files), encoding="utf-8")
    manifest = {key: value for key, value in result.items() if key not in {"sites", "targetExtensions", "ringGroups"}}
    manifest["files"] = files
    write_json(directory / "export-manifest.json", manifest)
    summary = ["# RingCentral - Export Site Configurations", "", f'- Account: {result["account"].get("name")}',
               f'- Account time zone: {timezone_label(result["account"].get("timeZone"))}',
               f'- Exported: {result["finishedAt"]}', "", "## Counts", ""]
    summary.extend(f"- {key}: {value}" for key, value in result["counts"].items())
    summary.extend(["", "## Notes", "", *[f"- {note}" for note in result["notes"]]])
    summary.extend(["", "## Unavailable endpoints", "", "Counts cover discovered data only. Failed listings have unknown counts; detail failures retain listing entries.", ""])
    summary.extend(f'- {e["label"]}: {e["message"]}' for e in result["errors"])
    summary.extend(["", "## Files", "", *[f"- {name}" for name in files], ""])
    (directory / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    return manifest


def run_export(opts, client=None):
    validate_selection(opts)
    log = Logger(opts.quiet)
    if opts.env_file:
        log.info(f"loaded {load_env_file(opts.env_file)} variable(s) from {opts.env_file}")
    server = opts.server or os.environ.get("RC_SERVER_URL") or SERVER
    rc = client if client is not None else RcClient(server, log)
    rc.login()
    started = datetime.now(timezone.utc).isoformat()
    account = rc.get(BASE)
    # Keep authentication responses and credentials out of exported metadata.
    account_info = {key: account.get(key) for key in ("id", "name", "mainNumber", "status", "regionalSettings")}
    ctx = {"errors": [], "notes": [], "log": log, "account": account_info}
    regional, regional_error = attempt(
        ctx, "account regional settings", lambda: rc.get("/restapi/v2/accounts/~/regional-settings")
    )
    account_info["regionalSettingsStatus"] = {
        "status": "incomplete" if regional_error else "complete", "error": regional_error
    }
    account_info["regionalSettings"] = regional if regional is not None else account.get("regionalSettings")
    account_info["timeZone"] = configured_timezone(
        ("Account regional settings", regional), ("Account info", account.get("regionalSettings"))
    )
    sites = export_sites(rc, opts, ctx)
    target_extensions = None
    if unresolved_target_ids(sites):
        target_extensions, _ = attempt(ctx, "extension listing for rule targets", lambda: rc.get_all(f"{BASE}/extension"))
    ring_groups = None
    if getattr(opts, "ring_group_details", False):
        ring_groups = fetch_ring_groups(rc, ctx, target_extension_ids(sites))
    if not account_info["timeZone"]["value"]:
        ctx["notes"].append("Account time zone was not returned. Schedules retain their configured wall-clock times; no browser/local time zone is assumed.")
    incomplete_details = sum(
        status["status"] != "complete" for site in sites
        for field in ("answeringRuleDetails", "ivrMenuDetails", "emergencyLocationDetails")
        for status in site[field]
    )
    counts = {"sites": len(sites), "siteMembers": sum(len(s["members"] or []) for s in sites),
              "answeringRules": sum(len(s["answeringRules"] or []) for s in sites),
              "customRules": sum(len(s["customRules"] or []) for s in sites),
              "ivrMenus": sum(len(s["ivrMenus"] or []) for s in sites),
              "emergencyLocations": sum(len(s["emergencyLocations"] or []) for s in sites),
              "incompleteDetails": incomplete_details,
              "incompleteRuleListings": sum(s["customRuleListingStatus"] != "complete" for s in sites),
              "unavailableEndpoints": len(ctx["errors"])}
    result = {"tool": "rc-config-export", "server": server, "account": account_info,
              "options": {"allSites": opts.all_sites, "siteIds": opts.site_ids, "siteNames": opts.site_names,
                          "envFile": "(set)" if opts.env_file else None, "open": opts.open},
              "startedAt": started, "finishedAt": datetime.now(timezone.utc).isoformat(),
              "counts": counts, "apiRequests": rc.requests, "apiRetries": rc.retries,
              "notes": ctx["notes"], "errors": ctx["errors"], "sites": sites, "targetExtensions": target_extensions, "ringGroups": ring_groups}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_%f")
    directory = opts.out.resolve() / f'{safe_name(account_info.get("name") or account_info.get("id"))}_{stamp}'
    manifest = write_outputs(directory, result, rc.responses)
    return directory, manifest


def main(argv=None):
    opts = parse_args(argv)
    try:
        directory, manifest = run_export(opts)
    except (ExportError, OSError, http.client.HTTPException) as error:
        print(f"export failed: {error}", file=sys.stderr)
        return 1
    print(f"\nExport saved: {directory}\nBrowser report: {directory / 'report.html'}")
    for key, value in manifest["counts"].items():
        print(f"  {label(key)}: {value}")
    if manifest["errors"]:
        print("INCOMPLETE: some data could not be read. Counts cover discovered records only; see the report warnings.")
    if opts.open:
        try:
            opened = webbrowser.open((directory / "report.html").as_uri())
            if not opened:
                print("WARN no browser could be launched; open the saved report manually.", file=sys.stderr)
        except (webbrowser.Error, OSError) as error:
            print(f"WARN browser launch failed: {error}; open the saved report manually.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
