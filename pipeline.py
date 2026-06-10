#!/usr/bin/env python3

import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv



COMPANIES_API_URL = "https://api.thecompaniesapi.com/v2/companies/similar"
PROSPEO_SEARCH_URL = "https://api.prospeo.io/search-person"
PROSPEO_ENRICH_URL = "https://api.prospeo.io/enrich-person"
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

SENDER_EMAIL = "pesalagovindacharudatta@gmail.com"
SENDER_NAME = "Govinda Charu datta"

API_DELAY_SECONDS = 1
DEFAULT_SIMILAR_COMPANY_COUNT = 5
STAGE1_FETCH_POOL_SIZE = 50
MAX_CONTACTS_PER_DOMAIN = 1

CSUITE_SENIORITIES = ("C-Suite", "Vice President")

PREFERRED_INDUSTRY_KEYWORDS = (
    "saas",
    "software",
    "internet",
    "technology",
    "ecommerce",
    "e-commerce",
    "fintech",
    "financial",
    "information-technology",
    "computer",
    "online",
    "retail",
    "payments",
    "commerce",
    "digital",
    "travel",
    "advertising",
    "media",
)

EXCLUDED_INDUSTRY_KEYWORDS = (
    "government",
    "higher-education",
    "mining",
    "construction",
    "insurance",
    "banking",
    "telecommunications",
    "transportation",
    "newspapers",
    "staffing",
    "wholesale",
    "airlines",
    "municipalities",
    "professional-organizations",
    "law-enforcement",
    "utilities",
    "real-estate",
)

EXCLUDED_DOMAIN_PATTERNS = (
    ".gov",
    ".mil",
    ".edu",
    ".gc.ca",
    ".canada.ca",
    "gov.",
    "looker.com",
    ".cloud.",
)


@dataclass
class LookalikeCompany:
    domain: str
    name: str
    industry: str
    employees: str
    score: int


@dataclass
class Prospect:
    """Stage 2 output: decision-maker identified, email not yet resolved."""

    name: str
    title: str
    company: str
    domain: str
    linkedin_url: str
    person_id: str


@dataclass
class Contact:
    """Stage 3 output: verified email resolved, ready for outreach."""

    name: str
    title: str
    email: str
    company: str
    linkedin_url: str




def log(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        safe = message.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        print(safe, flush=True)


def normalize_domain(domain: str) -> str:
    """Strip protocol, path, and www prefix from a domain input."""
    domain = domain.strip().lower()
    if "://" in domain:
        domain = urlparse(domain).netloc or urlparse(f"https://{domain}").path
    domain = domain.split("/")[0].split("?")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        log(f"Error: Missing {key} in .env file.")
        sys.exit(1)
    return value


def api_delay() -> None:
    time.sleep(API_DELAY_SECONDS)


def handle_rate_limit(response: requests.Response, service: str) -> None:
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        log(f"  Rate limit hit on {service}. Waiting {retry_after}s before retry...")
        time.sleep(int(retry_after))
        return
    response.raise_for_status()


def company_display_name(domain: str) -> str:
    base = domain.split(".")[0]
    return base.replace("-", " ").title()


def first_name(full_name: str) -> str:
    return full_name.strip().split()[0] if full_name.strip() else "there"


def stage1_skip_reason(domain: str, industry: str, seed_domain: str) -> str | None:
    """Return a human-readable reason when a lookalike should be excluded."""
    if not domain or "." not in domain:
        return "invalid or empty domain"

    if domain == seed_domain:
        return "same as seed domain"

    for pattern in EXCLUDED_DOMAIN_PATTERNS:
        if pattern in domain:
            return (
                f"domain matches excluded pattern '{pattern}' "
                "(government, institutional, or non-commercial host)"
            )

    labels = domain.split(".")
    if len(labels) > 3:
        return "domain has too many subdomain levels for reliable contact lookup"

    if len(labels) == 3 and labels[-2] in ("canada", "gov", "gc"):
        return "government or public-sector domain structure"

    industry_lower = (industry or "").lower()
    for keyword in EXCLUDED_INDUSTRY_KEYWORDS:
        if keyword in industry_lower:
            return f"industry '{industry}' is outside our target market"

    return None


def industry_is_preferred(industry: str) -> bool:
    industry_lower = (industry or "").lower()
    return any(keyword in industry_lower for keyword in PREFERRED_INDUSTRY_KEYWORDS)


def extract_company_metadata(company: dict[str, Any]) -> LookalikeCompany | None:
    """Build a lookalike record with firmographic fields from a Companies API object."""
    domain = extract_company_domain(company)
    if not domain:
        return None

    about = company.get("about") or {}
    meta = company.get("meta") or {}
    score_raw = meta.get("score")
    score = int(score_raw) if isinstance(score_raw, (int, float)) else 0

    return LookalikeCompany(
        domain=domain,
        name=str(about.get("name") or company_display_name(domain)),
        industry=str(about.get("industry") or "unknown"),
        employees=str(about.get("totalEmployees") or "unknown"),
        score=score,
    )


def explain_prospeo_search_failure(domain: str, search_data: dict[str, Any] | None) -> str:
    """Explain why a Prospeo domain search produced no usable prospects."""
    if not search_data:
        return (
            f"Prospeo API request failed for '{domain}' "
            "(network error, rate limit, or service unavailable)."
        )

    if search_data.get("error"):
        error_code = search_data.get("error_code", "API_ERROR")
        filter_error = search_data.get("filter_error", "").strip()

        if error_code == "INVALID_FILTERS":
            detail = (
                f" Prospeo says: {filter_error}" if filter_error else ""
            )
            return (
                f"Prospeo rejected '{domain}' as a search filter — "
                "this usually means the domain is government, institutional, "
                f"or not a standard commercial website.{detail}"
            )

        if error_code == "INSUFFICIENT_CREDITS":
            return "Prospeo credits exhausted — cannot search further."

        if error_code == "RATE_LIMITED":
            return "Prospeo rate limit exceeded for this search."

        return f"Prospeo returned error '{error_code}' for '{domain}'."

    return (
        f"Prospeo has no C-Suite or VP-level contacts indexed for '{domain}' "
        "in their database (company may be too small or poorly covered)."
    )


def explain_prospeo_linkedin_miss(domain: str, raw_count: int) -> str:
    return (
        f"Prospeo returned {raw_count} executive(s) at '{domain}', "
        "but none had a LinkedIn profile URL attached — "
        "LinkedIn is required before email resolution can run."
    )


def explain_prospeo_enrich_failure(prospect: Prospect, data: dict[str, Any] | None) -> str:
    if not data:
        return (
            f"Prospeo enrichment request failed for {prospect.name} "
            f"at {prospect.company} (network or rate-limit issue)."
        )

    if data.get("error"):
        error_code = data.get("error_code", "API_ERROR")
        if error_code == "NO_MATCH":
            return (
                f"No verified work email found for {prospect.name} "
                f"({prospect.title} at {prospect.company}) — "
                "Prospeo could not match this profile to a deliverable address."
            )
        if error_code == "INSUFFICIENT_CREDITS":
            return "Prospeo credits exhausted — cannot enrich further."
        return f"Prospeo enrichment error '{error_code}' for {prospect.name}."

    return (
        f"Prospeo enriched {prospect.name} but returned no verified email "
        "(address may be hidden, catch-all, or unverified)."
    )


def extract_company_domain(company: dict[str, Any]) -> str | None:
    """Pull a clean domain string from a Companies API company record."""
    candidates: list[Any] = [
        company.get("domain"),
        company.get("website"),
        (company.get("about") or {}).get("domain"),
        (company.get("about") or {}).get("website"),
    ]

    for candidate in candidates:
        if isinstance(candidate, dict):
            value = candidate.get("domain") or candidate.get("redirection")
        else:
            value = candidate
        if value:
            return normalize_domain(str(value))
    return None


def prospeo_post(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    service: str,
) -> dict[str, Any] | None:
    """POST to Prospeo with rate-limit retry and structured error handling."""
    headers = {"X-KEY": api_key, "Content-Type": "application/json"}

    for attempt in range(5):
        api_delay()
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
        except requests.RequestException as exc:
            log(f"  Warning: {service} request failed — {exc}")
            return None

        if response.status_code == 429:
            handle_rate_limit(response, service)
            if attempt < 4:
                continue
            log(f"  Warning: {service} still rate-limited after retries.")
            return None

        if response.status_code == 401:
            log("Error: Invalid PROSPEO_API_KEY.")
            sys.exit(1)

        if response.status_code == 400:
            body = response.json() if response.content else {}
            error_code = body.get("error_code", body.get("message", "Bad request"))
            if error_code == "NO_RESULTS":
                return {"error": False, "results": []}
            return {
                "error": True,
                "error_code": error_code,
                "filter_error": body.get("filter_error", ""),
            }

        try:
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            log(f"  Warning: {service} request failed — {exc}")
            return None

    return None


def extract_prospeo_email(person: dict[str, Any]) -> str:
    """Read a verified email from a Prospeo person object."""
    email_field = person.get("email")
    if isinstance(email_field, dict):
        if email_field.get("status") == "VERIFIED" and email_field.get("revealed"):
            return str(email_field.get("email", "")).strip()
        return ""
    return str(email_field or "").strip()



def stage1_find_lookalike_companies(seed_domain: str, api_key: str) -> list[str]:
    """Find similar companies for a seed domain with firmographic filtering."""
    log("\n")
    log("STAGE 1: Finding lookalike companies")
    log(f"Seed domain: {seed_domain}")
    # log(
    #     f"Fetching up to {STAGE1_FETCH_POOL_SIZE} candidates, "
    #     f"keeping the best {DEFAULT_SIMILAR_COMPANY_COUNT} "
    #     "(sorted by similarity score, tech/commerce industries preferred)."
    # )

    params = {
        "domains[]": seed_domain,
        "size": STAGE1_FETCH_POOL_SIZE,
        "sortKey": "meta.score",
        "sortOrder": "desc",
    }
    headers = {"Authorization": f"Basic {api_key}"}

    try:
        api_delay()
        response = requests.get(
            COMPANIES_API_URL,
            params=params,
            headers=headers,
            timeout=60,
        )

        if response.status_code == 429:
            handle_rate_limit(response, "The Companies API")
            api_delay()
            response = requests.get(
                COMPANIES_API_URL,
                params=params,
                headers=headers,
                timeout=60,
            )

        if response.status_code == 401:
            log("Error: Invalid COMPANIES_API_KEY.")
            sys.exit(1)

        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        log(f"Error: Companies API request failed — {exc}")
        sys.exit(1)

    companies = data.get("companies", [])
    candidates: list[LookalikeCompany] = []
    skipped: list[tuple[str, str]] = []

    for company in companies:
        record = extract_company_metadata(company)
        if not record:
            continue

        reason = stage1_skip_reason(record.domain, record.industry, seed_domain)
        if reason:
            skipped.append((record.domain, reason))
            continue

        candidates.append(record)

    candidates.sort(
        key=lambda item: (industry_is_preferred(item.industry), item.score),
        reverse=True,
    )

    selected = candidates[:DEFAULT_SIMILAR_COMPANY_COUNT]
    domains = [item.domain for item in selected]

    # if skipped:
    #     log(f"\nFiltered out {len(skipped)} weak or non-commercial candidates:")
    #     for domain, reason in skipped[:8]:
    #         log(f"  - {domain}: {reason}")
    #     if len(skipped) > 8:
    #         log(f"  ... and {len(skipped) - 8} more")

    log(
        f"\nSelected {len(domains)} lookalike companies "
        f"(from {len(companies)} returned by the API):"
    )
    for i, item in enumerate(selected, 1):
        preferred = (
            "preferred industry" if industry_is_preferred(item.industry) else "other industry"
        )
        log(
            f"  {i}. {item.domain} — {item.name} | "
            f"{item.industry} | {item.employees} employees | "
            f"score {item.score} ({preferred})"
        )

    if not domains:
        log(
            "Warning: No lookalike companies passed quality filters. "
            "Try a different seed domain with clearer firmographic data."
        )
        sys.exit(0)

    return domains



def stage2_find_decision_makers(domains: list[str], api_key: str) -> list[Prospect]:
    """Surface C-suite / VP decision-makers and their LinkedIn URLs per domain."""
    log("\n")
    log("STAGE 2: Finding decision makers + LinkedIn profiles")
   

    all_prospects: list[Prospect] = []
    seen_person_ids: set[str] = set()

    for i, domain in enumerate(domains, 1):
        log(f"\n[{i}/{len(domains)}] Searching {domain}...")

        search_data = prospeo_post(
            PROSPEO_SEARCH_URL,
            {
                "page": 1,
                "filters": {
                    "company": {"websites": {"include": [domain]}},
                    "person_seniority": {"include": list(CSUITE_SENIORITIES)},
                },
            },
            api_key,
            "Prospeo search",
        )

        results = search_data.get("results", []) if search_data else []
        if not search_data or search_data.get("error") or not results:
            log(f"  No results: {explain_prospeo_search_failure(domain, search_data)}")
            continue

        added = 0
        raw_count = len(results)
        missing_linkedin = 0

        for result in results:
            if added >= MAX_CONTACTS_PER_DOMAIN:
                break

            person = result.get("person", {})
            company = result.get("company", {})
            person_id = person.get("person_id")
            linkedin_url = (person.get("linkedin_url") or "").strip()

            if not person_id or person_id in seen_person_ids:
                continue
            if not linkedin_url:
                missing_linkedin += 1
                continue

            name = (
                person.get("full_name")
                or " ".join(
                    filter(None, [person.get("first_name"), person.get("last_name")])
                ).strip()
                or "Unknown"
            )
            title = person.get("current_job_title") or "Executive"
            company_name = company.get("name") or company_display_name(domain)

            seen_person_ids.add(person_id)
            prospect = Prospect(
                name=name,
                title=title,
                company=str(company_name),
                domain=domain,
                linkedin_url=linkedin_url,
                person_id=person_id,
            )
            all_prospects.append(prospect)
            added += 1
            log(f"    - {prospect.name} | {prospect.title} | {prospect.linkedin_url}")

        if not added:
            if missing_linkedin:
                log(f"  No results: {explain_prospeo_linkedin_miss(domain, raw_count)}")
            else:
                log(f"  No results: {explain_prospeo_search_failure(domain, search_data)}")

    log(f"\nTotal prospects collected: {len(all_prospects)}")

    if not all_prospects:
        log("No prospects found. Pipeline stopping before email resolution.")
        sys.exit(0)

    return all_prospects




def stage3_resolve_emails(prospects: list[Prospect], api_key: str) -> list[Contact]:
    """Resolve each prospect's LinkedIn profile into a verified work email."""
    log("\n")
    log("STAGE 3: Resolving verified work emails")
  

    contacts: list[Contact] = []
    seen_emails: set[str] = set()

    for i, prospect in enumerate(prospects, 1):
        log(f"\n[{i}/{len(prospects)}] Enriching {prospect.name} ({prospect.company})...")

        data = prospeo_post(
            PROSPEO_ENRICH_URL,
            {
                "only_verified_email": True,
                "enrich_mobile": False,
                "data": {"person_id": prospect.person_id},
            },
            api_key,
            "Prospeo enrich",
        )

        if not data or data.get("error"):
            log(f"  Skipped: {explain_prospeo_enrich_failure(prospect, data)}")
            continue

        email = extract_prospeo_email(data.get("person", {}))
        if not email:
            log(f"  Skipped: {explain_prospeo_enrich_failure(prospect, data)}")
            continue

        if email.lower() in seen_emails:
            log(
                f"  Skipped: duplicate email '{email}' — "
                f"already resolved for another contact in this run."
            )
            continue

        seen_emails.add(email.lower())
        contact = Contact(
            name=prospect.name,
            title=prospect.title,
            email=email,
            company=prospect.company,
            linkedin_url=prospect.linkedin_url,
        )
        contacts.append(contact)
        log(f"  Resolved: {contact.email}")

    log(f"\nTotal verified contacts: {len(contacts)}")

    if not contacts:
        log("No verified emails resolved. Pipeline stopping before send stage.")
        sys.exit(0)

    return contacts




def build_email_content(contact: Contact) -> tuple[str, str]:
    """Return (subject, html_body) for a personalized outreach email."""
    fname = first_name(contact.name)
    company = contact.company
    title = contact.title

    subject = f"Quick idea for {company}'s operations, {fname}"

    html_body = f"""<html>
<body style="font-family: Arial, sans-serif; color: #222; line-height: 1.6;">
<p>Hi {fname},</p>

<p>I came across your profile as <strong>{title}</strong> at <strong>{company}</strong>
and wanted to reach out with a brief, relevant note — not a mass email.</p>

<p>We help companies like {company} automate repetitive workflows with practical AI:
things like lead routing, reporting, customer follow-ups, and internal ops tasks that
still eat up hours each week. The goal is simple: free up your team to focus on growth
instead of manual busywork.</p>

<p>Given your role, I thought it might be worth a 15-minute conversation to see if there
are 1–2 high-impact automations we could implement quickly for {company}.</p>

<p>Would you be open to a short call next week? Happy to share a few examples tailored
to your team.</p>

<p>Best regards,<br>
{SENDER_NAME}</p>
</body>
</html>"""

    return subject, html_body


def show_send_summary(contacts: list[Contact]) -> None:
    log("\n")
    log("STAGE 4: Email send preview")
    
    log(f"Sender: {SENDER_NAME} <{SENDER_EMAIL}>")
    log(f"Recipients: {len(contacts)}\n")

    for i, contact in enumerate(contacts, 1):
        log(f"  {i}. {contact.name}")
        log(f"     Title:    {contact.title}")
        log(f"     Company:  {contact.company}")
        log(f"     Email:    {contact.email}")
        log(f"     LinkedIn: {contact.linkedin_url}")
        log("")


def stage4_send_emails(contacts: list[Contact], api_key: str) -> None:
    """Show summary, ask for confirmation, then send personalized emails."""
    show_send_summary(contacts)

    
    confirmation = input('Send all emails? Type "yes" or "no": ').strip().lower()

    if confirmation == "no":
        log("\nSend cancelled. No emails were sent.")
        return

    if confirmation != "yes":
        log('\nInvalid input. Type "yes" to send or "no" to cancel. No emails were sent.')
        return

    log("\nSending emails...\n")

    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
        "accept": "application/json",
    }

    sent = 0
    failed = 0

    for i, contact in enumerate(contacts, 1):
        subject, html_content = build_email_content(contact)

        payload = {
            "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
            "to": [{"email": contact.email, "name": contact.name}],
            "subject": subject,
            "htmlContent": html_content,
        }

        try:
            api_delay()
            response = requests.post(
                BREVO_API_URL,
                json=payload,
                headers=headers,
                timeout=60,
            )

            if response.status_code == 429:
                handle_rate_limit(response, "Brevo")
                api_delay()
                response = requests.post(
                    BREVO_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=60,
                )

            if response.status_code == 401:
                log("Error: Invalid BREVO_API_KEY.")
                sys.exit(1)

            if response.status_code >= 400:
                detail = response.text[:200]
                log(f"  [{i}/{len(contacts)}] Failed — {contact.email}: {detail}")
                failed += 1
                continue

            message_id = response.json().get("messageId", "ok")
            log(f"  [{i}/{len(contacts)}] Sent to {contact.name} <{contact.email}> (id: {message_id})")
            sent += 1

        except requests.RequestException as exc:
            log(f"  [{i}/{len(contacts)}] Failed — {contact.email}: {exc}")
            failed += 1

    log(f"\nDone. Sent: {sent}, Failed: {failed}")




def main() -> None:
    load_dotenv()

    companies_key = require_env("COMPANIES_API_KEY")
    prospeo_key = require_env("PROSPEO_API_KEY")
    brevo_key = require_env("BREVO_API_KEY")


    raw_domain = input("Enter seed company domain (e.g. stripe.com): ").strip()
    if not raw_domain:
        log("Error: A seed domain is required.")
        sys.exit(1)

    seed_domain = normalize_domain(raw_domain)
    if not seed_domain or "." not in seed_domain:
        log("Error: Please enter a valid domain (e.g. stripe.com).")
        sys.exit(1)

    domains = stage1_find_lookalike_companies(seed_domain, companies_key)
    prospects = stage2_find_decision_makers(domains, prospeo_key)
    contacts = stage3_resolve_emails(prospects, prospeo_key)
    stage4_send_emails(contacts, brevo_key)

    log("\nPipeline complete.")


if __name__ == "__main__":
    main()
