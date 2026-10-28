"""HTTP availability and booking using a session authenticated by Selenium."""

import random
import copy
import re
import time as clock
from usvisa.shared import audit
from dataclasses import dataclass
from datetime import date, time, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import requests
from bs4 import BeautifulSoup



class PortalError(RuntimeError):
    """The portal response cannot be used to prepare a booking."""


class AuthenticationExpired(PortalError):
    """A new browser login is required."""


class RateLimited(PortalError):
    """The portal requires a pause before another request."""

    def __init__(self, retry_after=None):
        super().__init__("Portal GET returned HTTP 429 (rate limited)")
        self.retry_after = 0
        if retry_after is not None:
            try:
                self.retry_after = max(0, int(retry_after))
            except (ValueError, TypeError):
                try:
                    until = parsedate_to_datetime(retry_after)
                    self.retry_after = max(0, (until - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass


class BookingNotVerified(PortalError):
    """A POST may have changed the appointment; do not automatically resubmit."""

    def __init__(self, message, *, day=None, slot_time=None, post_sent=None):
        super().__init__(message)
        self.day, self.slot_time, self.post_sent = day, slot_time, post_sent


@dataclass(frozen=True)
class BookingResult:
    date: date
    time: str
    dry_run: bool


@dataclass(frozen=True)
class BookingPending:
    date: date
    time: str


def acceptable_dates(dates, earliest, latest, exclusions):
    return [day for day in sorted(set(dates))
            if earliest <= day <= latest
            and not any(start <= day <= end for start, end in exclusions)]


def form_fields(form):
    """Serialize successful form controls, preserving repeated applicant fields."""
    fields = []
    for control in form.find_all(["input", "select", "textarea", "button"]):
        name = control.get("name")
        if not name or control.has_attr("disabled"):
            continue
        kind = control.get("type", "text").lower()
        if control.name == "button" or kind in {"submit", "button", "reset", "file", "image"}:
            continue
        if kind in {"checkbox", "radio"} and not control.has_attr("checked"):
            continue
        if control.name == "select":
            options = [item for item in control.find_all("option")
                       if not item.has_attr("disabled")]
            selected = [item for item in options if item.has_attr("selected")]
            if not selected and not control.has_attr("multiple"):
                selected = options[:1]
            fields.extend((name, item.get("value", item.get_text())) for item in selected)
        elif control.name == "textarea":
            fields.append((name, control.get_text()))
        else:
            fields.append((name, control.get("value", "on" if kind in {"checkbox", "radio"} else "")))
    submit = form.find("input", attrs={"type": "submit", "name": True})
    if submit is None:
        submit = form.find("button", attrs={"name": True, "type": "submit"})
    if submit is not None and not submit.has_attr("disabled"):
        fields.append((submit["name"], submit.get("value", submit.get_text(strip=True))))
    return fields


def replace_fields(fields, values):
    return [(name, value) for name, value in fields if name not in values] + list(values.items())


def appointment_values(text):
    """Extract date/time values from a labelled saved appointment."""
    text = " ".join(text.split())
    dates = set()
    for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        try:
            dates.add(date.fromisoformat(value))
        except ValueError:
            pass
    months = {month.lower(): index for index, month in enumerate((
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December"), 1)}
    month_pattern = "|".join(months)
    for pattern, month_first in (
        (rf"\b(\d{{1,2}})\s+({month_pattern}),?\s+(\d{{4}})\b", False),
        (rf"\b({month_pattern})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", True),
    ):
        for match in re.finditer(pattern, text, re.I):
            first, second, year = match.groups()
            month, day = (first, second) if month_first else (second, first)
            try:
                dates.add(date(int(year), months[month.lower()], int(day)))
            except ValueError:
                pass
    times = set()
    for match in re.finditer(r"\b(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*(AM|PM)\b)?", text, re.I):
        hour, minute = int(match[1]), int(match[2])
        if match[3]:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if match[3].upper() == "PM" else 0)
        try:
            times.add(time(hour, minute).strftime("%H:%M"))
        except ValueError:
            pass
    return dates, times


def appointment_matches(text, wanted_date, wanted_time):
    dates, times = appointment_values(text)
    return dates == {wanted_date} and times == {wanted_time}


class AppointmentClient:
    def __init__(self, session, appointment_url, facility_id, consulate, timeout=30,
                 account_url=None):
        self.session = session
        self.facility_id = facility_id
        self.consulate = consulate
        self.timeout = timeout
        self.account_url = account_url
        self.appointment_url = None
        self.appointment_params = {}
        self.schedule_id = None
        self._prepared_form = None
        self.current_appointment = None
        if appointment_url is not None:
            self._set_appointment_url(appointment_url)

    def _set_appointment_url(self, appointment_url):
        self._prepared_form = None
        self.appointment_url = appointment_url.rstrip("/")
        match = re.search(r"/schedule/(\d+)/appointment$", self.appointment_url)
        if not match:
            raise ValueError("Expected a schedule appointment URL")
        self.schedule_id = match[1]
        self.account_url = self.appointment_url.split("/schedule/")[0]

    @classmethod
    def from_driver(cls, driver, facility_id, consulate, timeout=30):
        session = requests.Session()
        session.headers.update({
            "User-Agent": driver.execute_script("return navigator.userAgent"),
            "Referer": driver.current_url,
        })
        for cookie in driver.get_cookies():
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain", urlsplit(driver.current_url).hostname),
                path=cookie.get("path", "/"), secure=cookie.get("secure", False),
                expires=cookie.get("expiry"),
            )
        # Only transfer authentication here; HTTP navigation starts after Chrome closes.
        return cls(session, None, facility_id, consulate, timeout,
                   account_url="https://ais.usvisa-info.com/en-ca/niv")

    def _portal_url(self, base, target):
        url = urljoin(base, target)
        parsed = urlsplit(url)
        account = urlsplit(self.account_url)
        if (parsed.scheme != account.scheme or parsed.netloc != account.netloc
                or not parsed.path.startswith(account.path + "/")):
            raise PortalError("Unexpected navigation outside the portal account")
        return url

    def prepare_session(self):
        """Discover the application and prepare its booking form through HTTP."""
        response = self._get(self.account_url)
        visited = set()
        for _ in range(8):
            soup = BeautifulSoup(response.text, "html.parser")
            schedules = set()
            for value in [response.url] + [link["href"] for link in soup.find_all("a", href=True)]:
                url = urljoin(response.url, value)
                parsed = urlsplit(url)
                match = re.fullmatch(r"/en-ca/niv/schedule/(\d+)(?:/.*)?", parsed.path)
                if parsed.netloc == urlsplit(self.account_url).netloc and match:
                    schedules.add(match[1])
            if len(schedules) > 1:
                raise PortalError("Multiple applications found; cannot choose a schedule automatically")
            if schedules:
                self._set_appointment_url(
                    f"{self.account_url}/schedule/{schedules.pop()}/appointment"
                )
                self._booking_form(prepare=True)
                return
            links = [link for link in soup.find_all("a", href=True)
                     if link.get_text(" ", strip=True) in {"Continue", "Schedule Appointment"}]
            if len(links) != 1:
                raise PortalError("Could not identify the application link after login")
            target = self._portal_url(response.url, links[0]["href"])
            if target in visited:
                raise PortalError("Portal application navigation repeated a page")
            visited.add(target)
            response = self._get(target)
        raise PortalError("Portal application navigation exceeded its step limit")

    def _prepare_booking_form(self, response, allow_post=True):
        for _ in range(8):
            soup = BeautifulSoup(response.text, "html.parser")
            if soup.find("form", id="appointment-form") is not None:
                return response
            links = [link for link in soup.find_all("a", href=True)
                     if link.get_text(" ", strip=True) in {"Continue", "Schedule Appointment"}]
            if len(links) == 1:
                target = self._portal_url(response.url, links[0]["href"])
                if not urlsplit(target).path.startswith(f"/en-ca/niv/schedule/{self.schedule_id}/"):
                    raise PortalError("Setup link belongs to an unexpected application")
                response = self._get(target)
                continue
            # Only submit an explicit consent screen, never an appointment form.
            forms = [form for form in soup.find_all("form")
                     if form.find("input", attrs={"type": "checkbox", "name": True}) is not None
                     and form.find(attrs={"name": re.compile(r"^appointments\[")}) is None
                     and form.find(attrs={"name": "commit", "value": "Continue"}) is not None]
            if len(forms) != 1:
                raise PortalError("Unsupported appointment setup page; booking form was not found")
            form = forms[0]
            target = self._portal_url(response.url, form.get("action") or response.url)
            method = form.get("method", "get").lower()
            if method == "get":
                # The portal's rescheduling-limit acknowledgement is a GET form,
                # with no CSRF token. Preserve it on later booking-form requests.
                checkboxes = form.find_all("input", attrs={"type": "checkbox", "name": True})
                if (urlsplit(target).path != urlsplit(self.appointment_url).path
                        or {item["name"] for item in checkboxes} != {"confirmed_limit_message"}):
                    raise PortalError("Unexpected GET acknowledgement form")
                fields = replace_fields(form_fields(form), {"confirmed_limit_message": "1"})
                response = self._get(target, params=fields)
                self.appointment_params = {"confirmed_limit_message": "1"}
                continue
            if not allow_post:
                raise PortalError("Appointment setup requires a consent POST")
            if (form.get("method", "get").lower() != "post"
                    or not urlsplit(target).path.startswith(f"/en-ca/niv/schedule/{self.schedule_id}/")):
                raise PortalError("Unexpected consent form action or method")
            fields = form_fields(form)
            for checkbox in form.find_all("input", attrs={"type": "checkbox", "name": True}):
                if not checkbox.has_attr("disabled"):
                    fields = replace_fields(fields, {checkbox["name"]: checkbox.get("value", "1")})
            if not dict(fields).get("authenticity_token"):
                raise PortalError("Consent form has no CSRF token")
            audit.event("consent_post_started")
            try:
                response = self.session.post(target, data=fields, timeout=self.timeout,
                                             headers={"Referer": response.url})
            except requests.RequestException as error:
                raise PortalError(f"Consent request failed ({type(error).__name__})") from error
            audit.event("consent_post_completed", status=response.status_code)
            self._check_response(response)
            # Fetch the actual appointment page after completing consent.
            response = self._get(self.appointment_url)
        raise PortalError("Appointment setup exceeded its step limit")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def _get(self, url, **kwargs):
        endpoint = re.sub(r"/schedule/\d+", "/schedule/<application>", urlsplit(url).path)
        started = clock.monotonic()
        audit.event("http_started", method="GET", endpoint=endpoint)
        try:
            response = self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as error:
            audit.event("http_failed", method="GET", endpoint=endpoint, error_type=type(error).__name__, elapsed_ms=round((clock.monotonic()-started)*1000))
            raise PortalError(f"Portal GET failed ({type(error).__name__})") from error
        audit.event("http_completed", method="GET", endpoint=endpoint, status=response.status_code, elapsed_ms=round((clock.monotonic()-started)*1000))
        self._check_response(response)
        return response

    def _check_response(self, response):
        if response.status_code == 429:
            raise RateLimited(response.headers.get("Retry-After"))
        if "/users/sign_in" in response.url or response.status_code in {401, 403}:
            self._prepared_form = None
            raise AuthenticationExpired("HTTP session expired or was rejected; a new login is needed")
        if response.status_code != 200:
            raise PortalError(f"Portal GET returned HTTP {response.status_code}")
        if re.search(r'<input\b[^>]*\bid=[\"\']user_email[\"\']', response.text, re.I):
            self._prepared_form = None
            raise AuthenticationExpired("Portal returned the login form")

    def _json(self, url, params):
        response = self._get(url, params=params, headers={
            "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
        })
        try:
            return response.json()
        except ValueError as error:
            # Response bodies can contain account details/tokens.
            raise PortalError("Portal returned invalid JSON instead of availability") from error

    def get_available_dates(self):
        payload = self._json(
            f"{self.appointment_url}/days/{self.facility_id}.json",
            {"appointments[expedite]": "false"},
        )
        if not isinstance(payload, list):
            raise PortalError("Unexpected dates response: expected a JSON array")
        try:
            return sorted({date.fromisoformat(item["date"]) for item in payload})
        except (KeyError, TypeError, ValueError) as error:
            raise PortalError("Unexpected date entries in availability response") from error

    def get_available_times(self, day):
        audit.event("times_requested", date=day)
        payload = self._json(
            f"{self.appointment_url}/times/{self.facility_id}.json",
            {"date": day.isoformat(), "appointments[expedite]": "false"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("available_times"), list):
            raise PortalError("Unexpected times response: expected available_times array")
        times = payload["available_times"]
        try:
            if any(not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value)
                   for value in times):
                raise ValueError
            normalized = sorted({time.fromisoformat(value).strftime("%H:%M") for value in times})
            audit.event("times_response", date=day, times=normalized, count=len(normalized), status=200)
            return normalized
        except ValueError as error:
            raise PortalError("Unexpected appointment time format") from error

    def _booking_form(self, prepare=False):
        # Prepare during login/session validation, outside the slot-to-POST path.
        # A failed refresh must not leave an older form available for submission.
        self._prepared_form = None
        options = {"params": self.appointment_params} if self.appointment_params else {}
        response = self._get(self.appointment_url, **options)
        response = self._prepare_booking_form(response, allow_post=prepare)
        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", id="appointment-form")
        if form is None:
            raise PortalError("Appointment form was not found; check the account setup in the browser")
        action = urljoin(response.url, form.get("action") or response.url)
        if action.rstrip("/") != self.appointment_url or form.get("method", "get").lower() != "post":
            raise PortalError("Unexpected appointment form action or method")
        fields = form_fields(form)
        values = dict(fields)
        csrf = values.get("authenticity_token")
        if not csrf:
            meta = soup.find("meta", attrs={"name": "csrf-token"})
            csrf = meta.get("content") if meta else None
            if csrf:
                fields = replace_fields(fields, {"authenticity_token": csrf})
        if not csrf:
            raise PortalError("Appointment form has no CSRF token")
        prefix = "appointments[consulate_appointment]"
        if not all(form.find(attrs={"name": f"{prefix}[{field}]"}) is not None
                   for field in ("facility_id", "date", "time")):
            raise PortalError("Appointment form has no consulate booking fields")
        for control in form.find_all(attrs={"required": True, "name": True}):
            if "[asc_appointment]" in control["name"] and not values.get(control["name"]):
                raise PortalError("This account requires an ASC appointment; complete its setup first")
        self._prepared_form = (tuple(fields), csrf)
        return self._prepared_form

    def check_session(self):
        """Validate a restored login with a read-only request for its booking form."""
        self._booking_form()

    def _saved_appointment_matches(self, html, day, slot_time, account_page=False):
        soup = BeautifulSoup(html, "html.parser")
        # Form values/scripts can echo a rejected submission, so ignore them.
        for element in soup.find_all(["form", "script", "style"]):
            element.decompose()
        if account_page:
            application = None
            for link in soup.find_all("a", href=True):
                if re.search(rf"/schedule/{self.schedule_id}(?:/|$)", urlsplit(link["href"]).path):
                    application = link.find_parent(class_="application")
                    if application is not None:
                        break
            if application is None:
                return False
            soup = application
        location_pattern = rf"\b{re.escape(self.consulate)}\b"
        locations = []
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) >= 2 and re.fullmatch(
                r"Consular\s+(?:Section\s+)?Location:?",
                cells[0].get_text(" ", strip=True), re.I,
            ):
                locations.append(" ".join(cell.get_text(" ", strip=True) for cell in cells[1:]))
        for label in soup.find_all(["strong", "b", "dt"]):
            if re.fullmatch(r"Consular\s+(?:Section\s+)?Location:?",
                            label.get_text(" ", strip=True), re.I):
                value = label.find_next_sibling("dd") if label.name == "dt" else label.parent
                if value is not None:
                    locations.append(value.get_text(" ", strip=True))
        # A separate location row must agree even if the date mentions a city.
        if locations and not all(re.search(location_pattern, text, re.I) for text in locations):
            return False
        location_confirmed = bool(locations)
        for summary in soup.select(".consular-appt"):
            text = summary.get_text(" ", strip=True)
            if (location_confirmed or re.search(location_pattern, text, re.I)) and appointment_matches(
                text, day, slot_time,
            ):
                return True
        # Match labelled consular date rows, not arbitrary dates (e.g. ASC dates).
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) >= 2 and re.search(
                r"Consular\s+(?:Section\s+Interview\s+Date|Appointment)",
                cells[0].get_text(" ", strip=True), re.I,
            ):
                text = " ".join(cell.get_text(" ", strip=True) for cell in cells[1:])
                if (location_confirmed or re.search(location_pattern, text, re.I)) and appointment_matches(
                    text, day, slot_time,
                ):
                    return True
        for label in soup.find_all(["strong", "b", "dt"]):
            if re.fullmatch(r"Consular\s+(?:Section\s+Interview\s+Date|Appointment):?",
                            label.get_text(" ", strip=True), re.I):
                if label.name == "dt":
                    value = label.find_next_sibling("dd")
                    text = value.get_text(" ", strip=True) if value else ""
                else:
                    text = label.parent.get_text(" ", strip=True)
                if (location_confirmed or re.search(location_pattern, text, re.I)) and appointment_matches(
                    text, day, slot_time,
                ):
                    return True
        return False

    def verify_booking(self, day, slot_time):
        response = self._get(self.account_url, headers={"Cache-Control": "no-cache"})
        matches = self._saved_appointment_matches(response.text, day, slot_time, account_page=True)
        audit.event("verification_page", source="account", matches=matches)
        return matches

    def verification_client(self):
        """Independent session for read-only verification alongside availability."""
        session = requests.Session()
        try:
            session.headers.update(self.session.headers)
            session.cookies = copy.deepcopy(self.session.cookies)
            session.verify = self.session.verify
            session.cert = self.session.cert
            session.proxies = self.session.proxies.copy()
            session.trust_env = self.session.trust_env
            return AppointmentClient(session, self.appointment_url, self.facility_id, self.consulate, self.timeout)
        except BaseException:
            session.close()
            raise

    def capture_current_appointment(self):
        """Read the saved appointment from its application on the account page."""
        response = self._get(self.account_url, headers={"Cache-Control": "no-cache"})
        current = self._current_appointment_from_response(response)
        self.current_appointment = current
        if current:
            audit.event("current_appointment", source="account", **current)
        else:
            audit.event("current_appointment", status="unavailable", level="warning")
        return current

    def _current_appointment_from_response(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        for element in soup.find_all(["form", "script", "style"]):
            element.decompose()
        application = None
        for link in soup.find_all("a", href=True):
            if re.search(rf"/schedule/{self.schedule_id}(?:/|$)", urlsplit(link["href"]).path):
                application = link.find_parent(class_="application")
                if application is not None:
                    break
        if application is None:
            return None
        soup = application
        summaries = [part.get_text(" ", strip=True) for part in soup.select(".consular-appt")]
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) >= 2 and re.search(r"Consular\s+(?:Section\s+Interview\s+Date|Appointment)",
                                           cells[0].get_text(" ", strip=True), re.I):
                summaries.append(" ".join(cell.get_text(" ", strip=True) for cell in cells[1:]))
        for label in soup.find_all(["strong", "b", "dt"]):
            if re.fullmatch(r"Consular\s+(?:Section\s+Interview\s+Date|Appointment):?", label.get_text(" ", strip=True), re.I):
                value = label.find_next_sibling("dd") if label.name == "dt" else label.parent
                if value is not None:
                    summaries.append(value.get_text(" ", strip=True))
        for text in summaries:
            dates, times = appointment_values(text)
            if len(dates) == len(times) == 1:
                day, slot_time = next(iter(dates)), next(iter(times))
                if self._saved_appointment_matches(str(soup), day, slot_time):
                    return {"date": day.isoformat(), "time": slot_time, "consulate": self.consulate}
        return None

    def book(self, day, dry_run=True, *, expires_at=None):
        if not dry_run and audit.read_claim(self.schedule_id) is not None:
            guard = audit.read_claim(self.schedule_id)
            audit.event("booking_blocked", reason="saved_booking_state")
            raise BookingNotVerified("Saved booking state blocks another POST; monitoring can continue.",
                                     day=guard.get("date"), slot_time=guard.get("time"))
        if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
            audit.event("shared_slot_expired", date=day)
            return None
        audit.event("booking_attempt_started", date=day, dry_run=dry_run)
        # Startup already validated the form and CSRF in this HTTP session.
        # Direct callers without that preparation still need one form GET.
        form_cached = self._prepared_form is not None
        fields, csrf = self._prepared_form if form_cached else self._booking_form()
        times = self.get_available_times(day)
        if not times:
            audit.event("booking_no_times", date=day, booking_post_sent=False,
                        prepared_form_cached=form_cached)
            return None
        # A received finding can expire while form/times GETs are in flight.
        # Recheck before preparing a result or reserving a booking submission.
        if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
            audit.event("shared_slot_expired", date=day)
            return None
        slot_time = random.choice(times)
        prefix = "appointments[consulate_appointment]"
        values = {f"{prefix}[facility_id]": str(self.facility_id),
                  f"{prefix}[date]": day.isoformat(), f"{prefix}[time]": slot_time}
        if any(name == "confirmed_limit_message" for name, _ in fields):
            values["confirmed_limit_message"] = "1"
        fields = replace_fields(fields, values)
        audit.event("booking_prepared", date=day, time=slot_time, consulate=self.consulate,
                    dry_run=dry_run, prepared_form_cached=form_cached,
                    applicant_count=sum(name == "appointments[applicant_ids][]" for name, _ in fields))
        if dry_run:
            audit.event("booking_dry_run", booking_post_sent=False)
            return BookingResult(day, slot_time, True)
        try:
            record = audit.event("booking_submission_pending", date=day, time=slot_time)
            audit.claim(self.schedule_id, day, slot_time, self.consulate,
                        attempt_id=uuid4().hex, trace_id=record.get("trace_id") if record else None)
            claimed = audit.read_claim(self.schedule_id)
        except FileExistsError as error:
            guard = audit.read_claim(self.schedule_id) or {}
            raise BookingNotVerified("A submission was already reserved; no POST was sent.",
                                     day=guard.get("date") or day, slot_time=guard.get("time") or slot_time) from error
        except OSError as error:
            raise PortalError("Could not persist booking state; no POST was sent.") from error
        if not claimed:
            raise PortalError("Booking state disappeared; no POST was sent.")
        audit.event("booking_post_started", date=day, time=slot_time, consulate=self.consulate)
        audit.http_body("booking_http_request",
                        lambda: requests.Request("POST", self.appointment_url, data=fields).prepare().body,
                        method="POST", date=day, time=slot_time)
        started = clock.monotonic()
        origin = urlsplit(self.appointment_url)
        try:
            response = self.session.post(self.appointment_url, data=fields, timeout=self.timeout,
                allow_redirects=False, headers={"Referer": self.appointment_url,
                "Origin": f"{origin.scheme}://{origin.netloc}", "X-CSRF-Token": csrf})
            outcome = f"HTTP {response.status_code}"
            audit.http_body("booking_http_response", lambda: response.text,
                            method="POST", date=day, time=slot_time, status=response.status_code)
            level = "warning" if response.status_code == 429 else "error" if response.status_code >= 400 else "info"
        except requests.RequestException as error:
            outcome, level = type(error).__name__, "error"
        # The response is diagnostic only. Never confirm, retry, or clear the block.
        audit.event("booking_post_outcome", outcome=outcome, level=level,
                    elapsed_ms=round((clock.monotonic() - started) * 1000))
        audit.update_claim(self.schedule_id, expected_claim=claimed, outcome=outcome)
        return BookingPending(day, slot_time)
