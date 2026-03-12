"""CalDAV adapter — Nextcloud calendar and task operations.

Reference: openclaw-nextcloud community skill (keithvassallomt)
All methods return explicit structured dicts with http_status, http_calls_made,
propfind_http_status, and response_body. Never uses raise_for_status().
Never synthesises success or failure — only raw HTTP status codes and bodies.

Invariants (enforced on every method):
  - _discover_calendar() always returns a dict, never None
  - All write methods include http_calls_made, http_status, response_body,
    propfind_http_status in return dict
  - If a PUT/DELETE was not attempted, the dict says so explicitly
  - Non-2xx PUT/DELETE → status="error" with the actual code

Model B note: operations candidates for future DSL frontmatter:
  list_calendars(), list_events(calendar, from_date, to_date),
  create_event(calendar, uid, summary, start, end, description),
  update_event(calendar, uid, summary, start, end, description),
  delete_event(calendar, uid),
  create_task(calendar, uid, summary, due, start, description, status),
  complete_task(calendar, uid),
  delete_task(calendar, uid)
"""

import os
import re
import httpx
from datetime import datetime, timezone
from urllib.parse import unquote

CALDAV_BASE = os.environ.get(
    "CALDAV_BASE", "http://nextcloud/remote.php/dav/calendars/digiant"
).rstrip("/")
WEBDAV_USER = os.environ.get("WEBDAV_USER", "digiant")
WEBDAV_PASS = os.environ.get("WEBDAV_PASS", "")

PROPFIND_CAL_XML = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:displayname/><c:calendar-description/><d:resourcetype/></d:prop>"
    "</d:propfind>"
)

# CalDAV REPORT query for listing events in a time range
_REPORT_XML_TMPL = (
    '<?xml version="1.0"?>'
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    "<c:filter>"
    '<c:comp-filter name="VCALENDAR">'
    '<c:comp-filter name="{comp_type}">'
    "{time_range}"
    "</c:comp-filter>"
    "</c:comp-filter>"
    "</c:filter>"
    "</c:calendar-query>"
)

_TIME_RANGE_TMPL = '<c:time-range start="{start}" end="{end}"/>'

# Path portion of CALDAV_BASE (no scheme/host), for href comparison in PROPFIND responses
_BASE_PATH = re.sub(r"^https?://[^/]+", "", CALDAV_BASE)
# Origin (scheme + host) for building absolute URLs from discovered href paths
_m = re.match(r"https?://[^/]+", CALDAV_BASE)
_ORIGIN: str = _m.group(0) if _m else ""


class CalDAVAdapter:
    def _auth(self):
        return (WEBDAV_USER, WEBDAV_PASS)

    async def _discover_calendar(self, client: httpx.AsyncClient, name: str) -> dict:
        """PROPFIND /remote.php/dav/calendars/digiant/ with Depth:1 to enumerate calendar slugs.

        Returns a dict with the full discovery result so callers always have the raw HTTP
        context — the PROPFIND status, the raw response body, and all calendars found.
        Never returns None; never synthesises error text.

        Return shape:
            {
                "url": str | None,              # absolute URL of best-matching collection
                "propfind_http_status": int,
                "propfind_response_body": str,  # raw XML (truncated to 3000 chars)
                "calendars_found": [
                    {"slug": str, "display_name": str, "url": str}
                ]
            }
        Matching priority: exact slug → exact display name → partial → first available.
        """
        r = await client.request(
            "PROPFIND", f"{CALDAV_BASE}/",
            auth=self._auth(),
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=PROPFIND_CAL_XML.encode(),
        )

        discovery: dict = {
            "url": None,
            "propfind_http_status": r.status_code,
            "propfind_response_body": r.text[:3000],
            "calendars_found": [],
        }

        if r.status_code not in (207, 200):
            return discovery

        name_lower = name.lower().strip()
        calendars: list[dict] = []

        for block in re.findall(r"<d:response>(.*?)</d:response>", r.text, re.DOTALL):
            href_m = re.search(r"<d:href>(.*?)</d:href>", block)
            if not href_m:
                continue
            href = href_m.group(1).rstrip("/")
            # Skip the root collection itself
            if href in (_BASE_PATH, _BASE_PATH + "/"):
                continue
            # Skip non-calendar resources (e.g. inbox, outbox, notification)
            if "<cal:calendar" not in block and "calendar" not in block.lower():
                # still include — let slug matching filter appropriately
                pass
            slug = unquote(href.split("/")[-1])
            if not slug:
                continue
            dn_m = re.search(r"<d:displayname>(.*?)</d:displayname>", block, re.DOTALL)
            display = dn_m.group(1).strip() if dn_m else ""
            calendars.append({"slug": slug, "display_name": display, "url": f"{_ORIGIN}{href}"})

        discovery["calendars_found"] = calendars

        # Exact slug or display name
        for cal in calendars:
            if cal["slug"].lower() == name_lower or cal["display_name"].lower() == name_lower:
                discovery["url"] = cal["url"]
                return discovery
        # Partial match
        for cal in calendars:
            if name_lower in cal["slug"].lower() or name_lower in cal["display_name"].lower():
                discovery["url"] = cal["url"]
                return discovery
        # Fallback: first available calendar (not inbox/outbox/trashbin)
        skip = {"inbox", "outbox", "trashbin", "contact_birthdays"}
        for cal in calendars:
            if cal["slug"].lower() not in skip:
                discovery["url"] = cal["url"]
                return discovery
        if calendars:
            discovery["url"] = calendars[0]["url"]
        return discovery

    async def list_calendars(self) -> dict:
        """PROPFIND to enumerate available calendars. Returns raw HTTP status + body."""
        propfind_url = f"{CALDAV_BASE}/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.request(
                "PROPFIND", propfind_url,
                auth=self._auth(),
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=PROPFIND_CAL_XML.encode(),
            )
        result = {
            "http_calls_made": [f"PROPFIND {propfind_url}"],
            "http_status": r.status_code,
            "response_body": r.text[:4000],
        }
        if r.status_code not in (207, 200):
            result["status"] = "error"
            result["error"] = f"CalDAV PROPFIND returned {r.status_code}"
        else:
            result["status"] = "ok"
            # Parse out a clean calendar list for the LLM
            calendars = []
            for block in re.findall(r"<d:response>(.*?)</d:response>", r.text, re.DOTALL):
                href_m = re.search(r"<d:href>(.*?)</d:href>", block)
                if not href_m:
                    continue
                href = href_m.group(1).rstrip("/")
                if href in (_BASE_PATH, _BASE_PATH + "/"):
                    continue
                slug = unquote(href.split("/")[-1])
                dn_m = re.search(r"<d:displayname>(.*?)</d:displayname>", block, re.DOTALL)
                display = dn_m.group(1).strip() if dn_m else ""
                calendars.append({"slug": slug, "display_name": display})
            result["calendars"] = calendars
        return result

    async def list_events(
        self,
        calendar: str,
        from_date: str = "",
        to_date: str = "",
    ) -> dict:
        """REPORT calendar-query to list VEVENT items in a calendar, optionally filtered
        by date range.

        from_date / to_date: ISO 8601 date strings (e.g. '2026-03-01').
        If omitted, lists all events (no time-range filter).
        Returns raw http_status from both PROPFIND (discovery) and REPORT.
        """
        propfind_url = f"{CALDAV_BASE}/"
        async with httpx.AsyncClient(timeout=30.0) as client:
            discovery = await self._discover_calendar(client, calendar)

            if discovery["url"] is None:
                slugs = [c["slug"] for c in discovery["calendars_found"]]
                return {
                    "status": "error",
                    "error": (
                        f"No calendar matching '{calendar}' found "
                        f"(available: {slugs}) — REPORT not attempted"
                    ),
                    "http_calls_made": [f"PROPFIND {propfind_url}"],
                    "propfind_http_status": discovery["propfind_http_status"],
                    "propfind_response_body": discovery["propfind_response_body"],
                    "http_status": None,
                    "response_body": None,
                }

            if from_date and to_date:
                # Convert to CalDAV UTC format
                try:
                    start_dt = datetime.fromisoformat(from_date).strftime("%Y%m%dT000000Z")
                    end_dt   = datetime.fromisoformat(to_date).strftime("%Y%m%dT235959Z")
                    time_range = _TIME_RANGE_TMPL.format(start=start_dt, end=end_dt)
                except ValueError:
                    time_range = ""
            else:
                time_range = ""

            report_xml = _REPORT_XML_TMPL.format(comp_type="VEVENT", time_range=time_range)
            r = await client.request(
                "REPORT", discovery["url"],
                auth=self._auth(),
                headers={
                    "Depth": "1",
                    "Content-Type": "application/xml",
                },
                content=report_xml.encode(),
            )

        events = []
        if r.status_code in (207, 200):
            for block in re.findall(r"<C:calendar-data[^>]*>(.*?)</C:calendar-data>", r.text, re.DOTALL):
                uid_m      = re.search(r"^UID:(.+)$", block, re.MULTILINE)
                summary_m  = re.search(r"^SUMMARY:(.+)$", block, re.MULTILINE)
                dtstart_m  = re.search(r"^DTSTART[^:]*:(.+)$", block, re.MULTILINE)
                dtend_m    = re.search(r"^DTEND[^:]*:(.+)$", block, re.MULTILINE)
                desc_m     = re.search(r"^DESCRIPTION:(.+)$", block, re.MULTILINE)
                events.append({
                    "uid":         uid_m.group(1).strip() if uid_m else "",
                    "summary":     summary_m.group(1).strip() if summary_m else "",
                    "start":       dtstart_m.group(1).strip() if dtstart_m else "",
                    "end":         dtend_m.group(1).strip() if dtend_m else "",
                    "description": desc_m.group(1).strip() if desc_m else "",
                })

        result: dict = {
            "calendar": calendar,
            "calendar_url": discovery["url"],
            "http_calls_made": [f"PROPFIND {propfind_url}", f"REPORT {discovery['url']}"],
            "propfind_http_status": discovery["propfind_http_status"],
            "http_status": r.status_code,
            "response_body": r.text[:2000],
        }
        if r.status_code not in (207, 200):
            result["status"] = "error"
            result["error"] = f"CalDAV REPORT returned {r.status_code}"
        else:
            result["status"] = "ok"
            result["events"] = events
            result["count"] = len(events)
        return result

    async def create_event(
        self,
        calendar: str,
        uid: str,
        summary: str,
        start: str,
        end: str,
        description: str = "",
    ) -> dict:
        """Create a VEVENT.

        Step 1: PROPFIND to discover real calendar slug.
        Step 2: PUT ICS to {calendar_url}/{uid}.ics.
        Returns actual HTTP status from both calls. PUT not attempted = says so explicitly.

        start/end: ISO 8601 strings e.g. '2026-03-10T10:00:00'
        """
        try:
            dt_start = datetime.fromisoformat(start).strftime("%Y%m%dT%H%M%S")
            dt_end   = datetime.fromisoformat(end).strftime("%Y%m%dT%H%M%S")
        except ValueError as e:
            return {
                "status": "error",
                "error": f"Invalid datetime format: {e}",
                "http_calls_made": [],
                "http_status": None,
                "response_body": None,
                "propfind_http_status": None,
            }

        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Escape commas and semicolons in text fields per RFC 5545
        safe_summary = summary.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        safe_desc    = description.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")

        ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Sovereign//EN\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:{now}\r\n"
            f"DTSTART:{dt_start}\r\n"
            f"DTEND:{dt_end}\r\n"
            f"SUMMARY:{safe_summary}\r\n"
            f"DESCRIPTION:{safe_desc}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )

        propfind_url = f"{CALDAV_BASE}/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            discovery = await self._discover_calendar(client, calendar)

            if discovery["url"] is None:
                if discovery["propfind_http_status"] not in (207, 200):
                    reason = (
                        f"PROPFIND returned HTTP {discovery['propfind_http_status']} "
                        "— PUT not attempted"
                    )
                else:
                    slugs = [c["slug"] for c in discovery["calendars_found"]]
                    reason = (
                        f"No calendar matching '{calendar}' found in PROPFIND response "
                        f"(available slugs: {slugs}) — PUT not attempted"
                    )
                return {
                    "status": "error",
                    "error": reason,
                    "http_calls_made": [f"PROPFIND {propfind_url}"],
                    "propfind_http_status": discovery["propfind_http_status"],
                    "propfind_response_body": discovery["propfind_response_body"],
                    "http_status": None,
                    "response_body": None,
                }

            event_url = f"{discovery['url']}/{uid}.ics"
            r = await client.put(
                event_url, auth=self._auth(),
                content=ics.encode(),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
            )

        result = {
            "uid": uid,
            "calendar": calendar,
            "calendar_url": discovery["url"],
            "http_calls_made": [f"PROPFIND {propfind_url}", f"PUT {event_url}"],
            "propfind_http_status": discovery["propfind_http_status"],
            "http_status": r.status_code,
            "response_body": r.text[:1000] if r.text else "",
        }
        if r.status_code not in (201, 204):
            result["status"] = "error"
            result["error"] = f"CalDAV PUT returned {r.status_code} — event not created"
        else:
            result["status"] = "ok"
        return result

    async def update_event(
        self,
        calendar: str,
        uid: str,
        summary: str,
        start: str,
        end: str,
        description: str = "",
    ) -> dict:
        """Update an existing VEVENT by overwriting its ICS (PUT to same UID path).

        Nextcloud CalDAV returns 204 on successful overwrite.
        Identical flow to create_event — PUT is idempotent on existing UID.
        """
        return await self.create_event(calendar, uid, summary, start, end, description)

    async def create_task(
        self,
        calendar: str,
        uid: str,
        summary: str,
        due: str = "",
        start: str = "",
        description: str = "",
        status: str = "NEEDS-ACTION",
    ) -> dict:
        """Create a VTODO (task).

        Same PROPFIND discovery flow as create_event.
        due/start: ISO 8601 strings (both optional).
        status: VTODO STATUS value — 'NEEDS-ACTION' | 'IN-PROCESS' | 'COMPLETED' | 'CANCELLED'
        """
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_summary = summary.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        safe_desc    = description.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")

        ics_lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Sovereign//EN",
            "BEGIN:VTODO",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
        ]
        if start:
            try:
                ics_lines.append(f"DTSTART:{datetime.fromisoformat(start).strftime('%Y%m%dT%H%M%S')}")
            except ValueError:
                pass
        if due:
            try:
                ics_lines.append(f"DUE:{datetime.fromisoformat(due).strftime('%Y%m%dT%H%M%S')}")
            except ValueError:
                pass
        ics_lines += [
            f"SUMMARY:{safe_summary}",
            f"DESCRIPTION:{safe_desc}",
            f"STATUS:{status}",
            "END:VTODO",
            "END:VCALENDAR",
        ]
        ics = "\r\n".join(ics_lines) + "\r\n"

        propfind_url = f"{CALDAV_BASE}/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            discovery = await self._discover_calendar(client, calendar)

            if discovery["url"] is None:
                if discovery["propfind_http_status"] not in (207, 200):
                    reason = (
                        f"PROPFIND returned HTTP {discovery['propfind_http_status']} "
                        "— PUT not attempted"
                    )
                else:
                    slugs = [c["slug"] for c in discovery["calendars_found"]]
                    reason = (
                        f"No calendar matching '{calendar}' found "
                        f"(available slugs: {slugs}) — PUT not attempted"
                    )
                return {
                    "status": "error",
                    "error": reason,
                    "http_calls_made": [f"PROPFIND {propfind_url}"],
                    "propfind_http_status": discovery["propfind_http_status"],
                    "propfind_response_body": discovery["propfind_response_body"],
                    "http_status": None,
                    "response_body": None,
                }

            task_url = f"{discovery['url']}/{uid}.ics"
            r = await client.put(
                task_url, auth=self._auth(),
                content=ics.encode(),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
            )

        result = {
            "uid": uid,
            "calendar": calendar,
            "calendar_url": discovery["url"],
            "http_calls_made": [f"PROPFIND {propfind_url}", f"PUT {task_url}"],
            "propfind_http_status": discovery["propfind_http_status"],
            "http_status": r.status_code,
            "response_body": r.text[:1000] if r.text else "",
        }
        if r.status_code not in (201, 204):
            result["status"] = "error"
            result["error"] = f"CalDAV PUT returned {r.status_code} — task not created"
        else:
            result["status"] = "ok"
        return result

    async def complete_task(self, calendar: str, uid: str) -> dict:
        """Mark a VTODO as COMPLETED.

        Fetches the existing ICS, replaces STATUS with COMPLETED,
        adds COMPLETED timestamp, PUTs back. Returns actual HTTP codes from all calls.
        """
        propfind_url = f"{CALDAV_BASE}/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            discovery = await self._discover_calendar(client, calendar)

            if discovery["url"] is None:
                slugs = [c["slug"] for c in discovery["calendars_found"]]
                return {
                    "status": "error",
                    "error": (
                        f"No calendar matching '{calendar}' found "
                        f"(available: {slugs}) — complete not attempted"
                    ),
                    "http_calls_made": [f"PROPFIND {propfind_url}"],
                    "propfind_http_status": discovery["propfind_http_status"],
                    "http_status": None,
                    "response_body": None,
                }

            task_url = f"{discovery['url']}/{uid}.ics"
            get_r = await client.get(task_url, auth=self._auth())

            if get_r.status_code != 200:
                return {
                    "status": "error",
                    "error": f"GET existing task returned {get_r.status_code}",
                    "http_calls_made": [f"PROPFIND {propfind_url}", f"GET {task_url}"],
                    "propfind_http_status": discovery["propfind_http_status"],
                    "http_status": get_r.status_code,
                    "response_body": get_r.text[:500],
                }

            # Patch STATUS and add COMPLETED timestamp
            now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ics = get_r.text
            ics = re.sub(r"STATUS:[^\r\n]+", f"STATUS:COMPLETED", ics)
            if "COMPLETED:" not in ics:
                ics = ics.replace("END:VTODO", f"COMPLETED:{now}\r\nEND:VTODO")
            if "PERCENT-COMPLETE:" in ics:
                ics = re.sub(r"PERCENT-COMPLETE:[^\r\n]+", "PERCENT-COMPLETE:100", ics)
            else:
                ics = ics.replace("END:VTODO", "PERCENT-COMPLETE:100\r\nEND:VTODO")

            put_r = await client.put(
                task_url, auth=self._auth(),
                content=ics.encode(),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
            )

        result = {
            "uid": uid,
            "calendar": calendar,
            "calendar_url": discovery["url"],
            "http_calls_made": [
                f"PROPFIND {propfind_url}",
                f"GET {task_url}",
                f"PUT {task_url}",
            ],
            "propfind_http_status": discovery["propfind_http_status"],
            "get_http_status": get_r.status_code,
            "http_status": put_r.status_code,
            "response_body": put_r.text[:500] if put_r.text else "",
        }
        if put_r.status_code not in (200, 201, 204):
            result["status"] = "error"
            result["error"] = f"CalDAV PUT (complete) returned {put_r.status_code}"
        else:
            result["status"] = "ok"
        return result

    async def delete_task(self, calendar: str, uid: str) -> dict:
        """Delete a VTODO. Delegates to delete_event (identical flow)."""
        return await self.delete_event(calendar, uid)

    async def delete_event(self, calendar: str, uid: str) -> dict:
        """Delete a VEVENT or VTODO.

        Step 1: PROPFIND to discover the real calendar slug.
        Step 2: DELETE {calendar_url}/{uid}.ics.
        Returns actual HTTP status from both calls. DELETE not attempted = says so explicitly.
        """
        propfind_url = f"{CALDAV_BASE}/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            discovery = await self._discover_calendar(client, calendar)

            if discovery["url"] is None:
                if discovery["propfind_http_status"] not in (207, 200):
                    reason = (
                        f"PROPFIND returned HTTP {discovery['propfind_http_status']} "
                        "— DELETE not attempted"
                    )
                else:
                    slugs = [c["slug"] for c in discovery["calendars_found"]]
                    reason = (
                        f"No calendar matching '{calendar}' found "
                        f"(available: {slugs}) — DELETE not attempted"
                    )
                return {
                    "status": "error",
                    "error": reason,
                    "http_calls_made": [f"PROPFIND {propfind_url}"],
                    "propfind_http_status": discovery["propfind_http_status"],
                    "propfind_response_body": discovery["propfind_response_body"],
                    "http_status": None,
                    "response_body": None,
                }

            event_url = f"{discovery['url']}/{uid}.ics"
            r = await client.delete(event_url, auth=self._auth())

        result = {
            "uid": uid,
            "calendar_url": discovery["url"],
            "http_calls_made": [f"PROPFIND {propfind_url}", f"DELETE {event_url}"],
            "propfind_http_status": discovery["propfind_http_status"],
            "http_status": r.status_code,
            "response_body": r.text[:1000] if r.text else "",
        }
        if r.status_code not in (200, 204):
            result["status"] = "error"
            result["error"] = f"CalDAV DELETE returned {r.status_code}"
        else:
            result["status"] = "ok"
        return result
