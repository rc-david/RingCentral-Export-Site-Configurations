# RingCentral - Export Site Configurations

> **Please note:** This is an independent project built on RingCentral's public REST API. It is not an official RingCentral product or tool, is not supported by RingCentral, and will not be maintained by RingCentral. Review the code and your organization's policies before running it against a production account.

Site export to a **searchable offline browser report**, JSON, and CSV.
Requires **Python 3.10+**; uses only the standard library. No pip packages,
web server, or account configuration changes.

## Scope: what is collected

For every selected site:

- Site details, including every returned address, regional setting, operator, caller ID,
  and other configuration field.
- Site extension record, business hours, and phone numbers.
- **All answering rules**: custom, business-hours, and after-hours. Each rule is fetched
  individually, including disabled rules, with all returned conditions, caller IDs,
  called numbers, schedules, actions, transfer/forwarding destinations, voicemail,
  greetings, and any other nested fields.
- Site member listings, including users, queues, and IVR extensions.
- Site IVR settings/top menu and **full IVR menu definitions**, including actions and prompts.
- Emergency-location listings and **individual location details**.

All supported listings are paginated. Full API objects are preserved rather than
restricting exports to a fixed set of known fields.

This is **not an export of each member extension's configuration**. Queues may appear
in a site's member list, but their queue-specific configuration, presence, and overflow
settings are not requested. Greetings/prompts include the returned metadata and text,
not downloaded audio binaries. "All data" means all fields returned by the listed
configuration endpoints, not every RingCentral account resource or Admin Portal setting.

## Ring group details (optional)

Add `--ring-group-details` to look one level deeper: for every extension that a custom
rule transfers to (typically a ring group user), the export reads that extension, its
devices, and its work-hours ring settings, then reads each ring member's extension and
devices. Ring settings come from the new call handling API
(`state-rules/work-hours`); accounts that have not been migrated fall back to the legacy
business-hours rule. This costs several extra read-only API calls per target and per
member, so it is off by default. IVR menu targets are skipped because they have no
devices or ring settings.

In `report.html`, clicking a rule (on the Rules or Number routing tab) shows its
**Ring group members & devices**: ring order, co-worker name and extension, device
name and model, MAC/serial, status and phone-line number (DID). Without the flag, the
panel says the data was not collected.

```powershell
python .\rc-config-export.py --env-file rc-config-export.env --all-sites --ring-group-details --out .\exports
```

## Credentials and permissions

Use your existing credentials file unchanged, or create one:

```powershell
Copy-Item .\rc-config-export.env.example .\rc-config-export.env
```

Configure these values privately:

```text
RC_SERVER_URL=https://platform.ringcentral.com
RC_CLIENT_ID=your-client-id
RC_CLIENT_SECRET=your-client-secret
RC_JWT=your-admin-jwt
```

The application must use JWT authentication and the **ReadAccounts** application scope.
The JWT owner also needs the appropriate **user role and access scope**:

| API failure | Required access |
| --- | --- |
| `CMN-419` reading another extension's rules/business hours | `ReadUserAnsweringRules` with extended scope covering the site extension |
| `CMN-408` reading IVR menus/settings | `AutoReceptionist` user permission |
| `CMN-408` reading emergency locations | `ReadEmergencyLocations` user permission |

App scopes and user permissions are separate. Use an authorized administrator or a
properly scoped custom role; the script cannot bypass permissions. See the
[RingCentral permissions guide](https://developers.ringcentral.com/guide/basics/permissions).

Existing environment variables override the credentials file. If `RC_ACCESS_TOKEN`
is set, it is used instead of initial JWT login. Authenticate again after permission
changes. A token rejected with 401 is refreshed via JWT once when JWT credentials exist.

Only authentication uses POST; all configuration reads use GET. Tokens, JWT credentials,
and client secrets are not included in export metadata or raw response files.

## Run

Check Python:

```powershell
python --version
```

Your selected site:

```powershell
python .\rc-config-export.py --env-file rc-config-export.env --site-id 123456 --out .\exports
```

All sites:

```powershell
python .\rc-config-export.py --env-file rc-config-export.env --all-sites --out .\exports
```

Multiple sites, by ID and/or exact name:

```powershell
python .\rc-config-export.py --env-file rc-config-export.env --site-id 123456 --site-name "London" --site-name "Main Site" --out .\exports
```

- Explicitly choose `--all-sites` **or** one or more `--site-id` / `--site-name` selectors.
- Names are exact and case-sensitive; quote names containing spaces.
- IDs are site IDs, not extension numbers or site codes.
- Combined selectors export their union without duplicates.
- Unknown IDs/names fail. Ambiguous names require selection by ID.
- Main Site uses the ID `main-site` and company-level rules/settings.
- When site discovery returns 400/403/404 on its first page, a company-level Main Site
  fallback is exported. The report explicitly marks discovery as incomplete: a denied
  listing does **not** prove multi-site is disabled or that all sites have been found.

The browser report opens automatically. It is designed for exports with thousands of
rules, rather than stacking one large card for every rule:

- **Compact table**: site, listing order, rule name/ID, type, enabled state, schedule,
  action, destination, and completeness status.
- **Resizable columns**: drag the right edge of any column header, or focus its
  resize handle and use Left/Right arrows (Shift for larger steps). Widths remain
  set while filtering, paging, or switching categories in the current report tab.
  Use **Reset column widths** to restore the active category's defaults.
  Columns have readable starting widths and scroll horizontally instead of breaking
  words. Longer values show an ellipsis and hover text; full values remain in details.
- **Schedule rows**: each weekday occupies its own line within the same rule, with
  that day's time ranges together. Each dated range also gets its own line.
- **Separate detail panel**: click a record name to inspect identity/status, compact
  schedules, caller conditions, routing, voicemail, greetings, and additional fields
  in grouped columns. Full returned fields and raw JSON are collapsed by default.
- **Category tabs**: Rules, Site settings, Members, Phone numbers, IVR menus, and
  Emergency locations. Site configuration is no longer buried beneath all rule cards.
- **Search and filters**: search every returned field in the active category across
  all pages; filter by site and detail status, and by rule type/enabled state.
- **Page size**: choose **10, 50, 100, 500, or All**; default is 50. Previous/Next
  changes pages without rendering thousands of detail panels. The All option renders
  all matching table rows, not all expanded record details.
- **Print filtered results**: prints every matching table row across all pages and the
  selected record's detail panel. It does not expand every rule into a separate card.

Keep `report-template.html` next to the Python script; it is embedded into the generated
standalone report, so the exported report needs no supporting files to display.

Add `--no-open` for unattended runs, `--quiet` to suppress progress, or `--help` for usage.
If Windows resolves Python differently, use `py` instead of `python`.

### Calendar, times, and time zones

- Report weekdays and months are capitalized, with friendly times such as
  **Monday, 9:00 AM - 5:00 PM** and **December 25, 2026**.
- The account time zone is shown in the report header and text summary. It is read
  from `GET /restapi/v2/accounts/~/regional-settings` (`ReadAccounts`), with returned
  account info or Main Site discovery regional settings as fallbacks. The logged-in
  user's personal time zone is never substituted for the company setting.
- Sites can have different time zones. Each rule's table row and selected record
  identify the site's zone, using site detail, extension, then discovery settings.
  If missing, the account fallback is explicitly labeled; if neither is available,
  the zone is **Not returned**. Unavailable regional-setting requests remain visible
  in diagnostics.
- Weekly schedules and dates without offsets are local wall-clock values; they are
  never shifted. Offset-bearing timestamps are converted only when the returned
  zone has a browser-supported IANA name. Otherwise their original offset is shown;
  fixed UTC offsets are not guessed from API bias values (which cannot model DST).
  The export timestamp uses the account's IANA zone when available, otherwise
  explicitly labeled UTC, never the browser's default zone.
- Raw JSON and `record.*` CSV values remain exactly as returned, including original
  day keys and 24-hour times. Record CSVs include separate `timeZone` and
  `timeZoneSource` columns for context.

## Outputs

A new uniquely named account/timestamp folder is created for each export; existing
exports are never overwritten.

| Output | Contents |
| --- | --- |
| `report.html` | Compact paginated offline tables, filters, category tabs, and selected-record detail panel; opens automatically |
| `sites.json` | Full site configuration, full rule/menu/location definitions, listings, and completeness metadata |
| `sites.csv` | Site summary, rule counts, discovery status, and separate columns for site settings/business hours/IVR settings/collection diagnostics |
| `site-answering-rules.csv` | Every rule, including built-in rules |
| `site-custom-rules.csv` | Custom rules, including disabled ones |
| `number-routing.csv` | One row per custom rule and called number, showing where calls go: the target extension's name, extension number and type (for example a ring group, user or IVR menu), or the numbers a rule forwards to. Targets are matched by extension ID against site members, then the account extension list. Also shown as the **Number routing** tab in `report.html` |
| `ring-group-details.csv` | Only with `--ring-group-details`. One row per ring entry for each extension that a custom rule transfers to: ring order and duration, the entry type (co-worker, desktop apps, mobile apps, device, external number), and for co-workers their name, extension number, and each device with its model, serial/MAC (hardware phones only), status and phone-line number. The ring group's own devices follow as separate rows. Also shown as the **Ring group devices** tab in `report.html` |
| `site-members.csv` | All returned site member records |
| `site-phone-numbers.csv` | Site phone-number records |
| `site-ivr-menus.csv` | Full IVR menu definitions |
| `emergency-locations.csv` | Full emergency-location definitions |
| `raw/sites/*.json` | One complete assembled object per selected site |
| `raw/responses/*.json` | Verbatim successful configuration API response objects |
| `raw/responses/index.json` | Endpoint/query mapping for raw responses; authentication is excluded |
| `export-manifest.json` | Selected scope, counts, notes, errors, request/retry counts, and output files |
| `SUMMARY.md` | Text summary and unavailable endpoints |

CSV files are UTF-8 with a BOM for Excel. Record CSVs start with `siteId`, `siteName`,
`timeZone`, `timeZoneSource`,
listing `order`, record `id`, `name`, `type`, `enabled`, `detailStatus`, and `detailError`.
**Returned fields now have their own columns instead of one enormous JSON cell.**
Examples:

- Phone numbers: `record.phoneNumber`, `record.location`, `record.paymentType`,
  `record.usageType`, `record.status`, `record.country.name`, etc.
- Rules: `record.callHandlingAction`, `record.schedule.weeklyRanges.saturday[0].from`,
  `record.schedule.weeklyRanges.saturday[0].to`, `record.transfer.extension.id`, etc.
- Nested forwarding: `record.forwarding.rules[0].forwardingNumbers[0].phoneNumber`.

Array indices are zero-based and preserve the association between values belonging to
the same caller, schedule range, or forwarding target. Columns are the union of all
returned fields; values present only in later records are not dropped. Empty collections
still produce metadata headers. Empty objects/arrays are represented as `{}` / `[]`;
unknown or absent scalar values are blank.

The `details` JSON column was removed from all CSVs. Site CSV excludes record collections
already represented in their own files, avoiding thousands of rule columns in one site
row. Complete original objects remain unchanged in `sites.json` and raw JSON.

## Incomplete data and rate limits

- Unavailable endpoint reads log warnings and are retained in report/manifest diagnostics.
- Failed listings have unknown counts (blank in site CSV), not zero.
- Failed detail reads retain the corresponding listing entry marked `incomplete`.
- Per-field `dataStatus` and per-record `answeringRuleDetails`, `customRuleDetails`,
  `ivrMenuDetails`, and `emergencyLocationDetails` identify what was actually read.
- Counts cover discovered records only; they do not imply unavailable endpoints are empty.
- Reads are sequential to simplify rate-limit handling. The client honors learned
  rate-limit groups, retries 429 using Retry-After, retries temporary server/network
  failures, and reports page-limit exhaustion instead of silently truncating.

## Privacy and development checks

Treat every export, including HTML, as confidential. The HTML makes no API requests
and loads no external assets. Account-wide discovery responses may contain records
from other sites in `raw/responses`; assembled site outputs are limited to your selection.

Run offline tests without credentials or network access:

```powershell
python -B -m unittest -v test_rc_config_export
```
