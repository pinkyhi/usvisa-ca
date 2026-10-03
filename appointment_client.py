"""HTTP availability and booking using a session authenticated by Selenium."""

import re
from dataclasses import dataclass
from datetime import date, time
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


class PortalError(RuntimeError):
    """The portal response cannot be used to prepare a booking."""


class AuthenticationExpired(PortalError):
    """A new browser login is required."""


class BookingNotVerified(PortalError):
    """A POST may have changed the appointment; do not automatically resubmit."""


@dataclass(frozen=True)
class BookingResult:
    date: date
    time: str
    dry_run: bool


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


def appointment_matches(text, wanted_date, wanted_time):
    """Match only a labelled saved appointment, never a booking form value."""
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
    return dates == {wanted_date} and times == {wanted_time}


class AppointmentClient:
    def __init__(self, session, appointment_url, facility_id, consulate, timeout=30):
        self.session = session
        self.appointment_url = appointment_url.rstrip("/")
        self.facility_id = facility_id
        self.consulate = consulate
        self.timeout = timeout
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
        return cls(session, driver.current_url, facility_id, consulate, timeout)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def _get(self, url, **kwargs):
        try:
            response = self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as error:
            raise PortalError(f"Portal GET failed ({type(error).__name__})") from error
        if "/users/sign_in" in response.url or response.status_code in {401, 403}:
            raise AuthenticationExpired("HTTP session expired or was rejected; a new login is needed")
        if response.status_code != 200:
            raise PortalError(f"Portal GET returned HTTP {response.status_code}")
        if re.search(r'<input\b[^>]*\bid=[\"\']user_email[\"\']', response.text, re.I):
            raise AuthenticationExpired("Portal returned the login form")
        return response

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
            return sorted({time.fromisoformat(value).strftime("%H:%M") for value in times})
        except ValueError as error:
            raise PortalError("Unexpected appointment time format") from error

    def _booking_form(self):
        response = self._get(self.appointment_url)
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
        return fields, csrf

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
        for url, account_page in (
            (self.appointment_url + "/instructions", False),
            (self.account_url, True),
        ):
            try:
                response = self._get(url, headers={"Cache-Control": "no-cache"})
            except PortalError:
                continue
            if not account_page and urlsplit(response.url).path.rstrip("/") != urlsplit(url).path:
                account_page = True
            if self._saved_appointment_matches(response.text, day, slot_time, account_page):
                return True
        return False

    def book(self, day, dry_run=True):
        # GET the form before querying times so cookie/CSRF rotation is retained.
        fields, csrf = self._booking_form()
        times = self.get_available_times(day)
        if not times:
            return None
        slot_time = times[0]
        prefix = "appointments[consulate_appointment]"
        values = {
            f"{prefix}[facility_id]": str(self.facility_id),
            f"{prefix}[date]": day.isoformat(),
            f"{prefix}[time]": slot_time,
        }
        if any(name == "confirmed_limit_message" for name, _ in fields):
            values["confirmed_limit_message"] = "1"
        fields = replace_fields(fields, values)
        result = BookingResult(day, slot_time, dry_run)
        if dry_run:
            return result
        origin = urlsplit(self.appointment_url)
        try:
            response = self.session.post(
                self.appointment_url, data=fields, timeout=self.timeout,
                headers={"Referer": self.appointment_url,
                         "Origin": f"{origin.scheme}://{origin.netloc}",
                         "X-CSRF-Token": csrf},
            )
            outcome = f"HTTP {response.status_code}"
        except requests.RequestException as error:
            # A timeout does not prove the server failed to save the booking.
            outcome = type(error).__name__
        if self.verify_booking(day, slot_time):
            return result
        raise BookingNotVerified(
            f"Booking POST completed with {outcome}, but {day} at {slot_time} "
            f"in {self.consulate} could not be verified. Check the appointment "
            "in your account before restarting; no automatic booking retry was made."
        )
