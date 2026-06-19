# main.py
# ================================================================
# Email Threat Intelligence Aggregator — Proof of Concept
# Sheridan College Capstone Project
#
# This single file contains everything the program needs:
#   1. Load API keys from the .env file
#   2. Query AbuseIPDB for the sender IP
#   3. Query Google Safe Browsing for the URL
#   4. Calculate a combined 0-100 risk score
#   5. Print a formatted threat intelligence report
# ================================================================

import os
import requests
from dotenv import load_dotenv
from colorama import init, Fore, Style

# ----------------------------------------------------------------
# SETUP
# load_dotenv() reads your .env file and makes ABUSEIPDB_API_KEY
# and GOOGLE_SAFE_BROWSING_API_KEY available via os.getenv().
# ----------------------------------------------------------------
load_dotenv()
init(autoreset=True)   # Makes colorama colour codes work on Windows


# ================================================================
# SECTION 1 — API KEYS
# We read from the environment (loaded by dotenv above).
# The program exits immediately if a key is missing.
# ================================================================

ABUSEIPDB_API_KEY          = os.getenv("ABUSEIPDB_API_KEY")
GOOGLE_SAFE_BROWSING_API_KEY = os.getenv("GOOGLE_SAFE_BROWSING_API_KEY")

if not ABUSEIPDB_API_KEY:
    raise EnvironmentError(
        "ABUSEIPDB_API_KEY not found. "
        "Make sure your .env file exists and contains the key."
    )

if not GOOGLE_SAFE_BROWSING_API_KEY:
    raise EnvironmentError(
        "GOOGLE_SAFE_BROWSING_API_KEY not found. "
        "Make sure your .env file exists and contains the key."
    )


# ================================================================
# SECTION 2 — ABUSEIPDB
# Queries the AbuseIPDB API with the sender IP address.
# AbuseIPDB tracks IPs reported for spam, hacking, etc.
# API docs: https://docs.abuseipdb.com/#check-endpoint
# ================================================================

ABUSEIPDB_ENDPOINT = "https://api.abuseipdb.com/api/v2/check"


def check_ip(ip_address: str) -> dict:
    """
    Query AbuseIPDB for information about an IP address.

    Parameters:
        ip_address (str): The IP to look up, e.g. "192.168.1.1"

    Returns:
        dict: Threat data fields, or {"success": False, "error": "..."}.
    """
    headers = {
        "Key":    ABUSEIPDB_API_KEY,   # API key goes in the request header
        "Accept": "application/json",  # We want JSON back, not HTML
    }
    params = {
        "ipAddress":    ip_address,
        "maxAgeInDays": "90",   # Only count reports from the last 90 days
        "verbose":      "",     # Request extra detail
    }

    try:
        response = requests.get(ABUSEIPDB_ENDPOINT, headers=headers, params=params, timeout=10)

        if response.status_code == 200:
            raw = response.json()["data"]   # The useful part of the JSON response
            return {
                "success":               True,
                "ip":                    raw.get("ipAddress", ip_address),
                "abuse_confidence_score": raw.get("abuseConfidenceScore", 0),
                "total_reports":         raw.get("totalReports", 0),
                "isp":                   raw.get("isp", "Unknown"),
                "domain":                raw.get("domain", "Unknown"),
                "country":               raw.get("countryCode", "Unknown"),
                "usage_type":            raw.get("usageType", "Unknown"),
                "last_reported":         raw.get("lastReportedAt", "Never"),
                "is_whitelisted":        raw.get("isWhitelisted", False),
            }
        elif response.status_code == 401:
            return {"success": False, "error": "Invalid AbuseIPDB API key. Check your .env file."}
        elif response.status_code == 422:
            return {"success": False, "error": f"Invalid IP address format: '{ip_address}'"}
        elif response.status_code == 429:
            return {"success": False, "error": "AbuseIPDB rate limit reached. Wait and try again."}
        else:
            return {"success": False, "error": f"AbuseIPDB returned HTTP {response.status_code}"}

    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Cannot reach AbuseIPDB. Check your internet connection."}
    except requests.exceptions.Timeout:
        return {"success": False, "error": "AbuseIPDB request timed out after 10 seconds."}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


# ================================================================
# SECTION 3 — GOOGLE SAFE BROWSING
# Queries Google's threat list to check whether a URL is malicious.
# API docs: https://developers.google.com/safe-browsing/v4/lookup-api
# ================================================================

GSB_ENDPOINT = "https://safebrowsing.googleapis.com/v4/threatMatches:find"

# Maps Google's threat type codes to plain-English descriptions
THREAT_DESCRIPTIONS = {
    "MALWARE":                         "Malware — the URL hosts malicious software",
    "SOCIAL_ENGINEERING":              "Phishing / Social Engineering — designed to steal credentials",
    "UNWANTED_SOFTWARE":               "Unwanted Software — hosts potentially harmful programs",
    "POTENTIALLY_HARMFUL_APPLICATION": "Potentially Harmful App — risky mobile application",
    "THREAT_TYPE_UNSPECIFIED":         "Unspecified Threat — flagged but type is unknown",
}


def check_url(url: str) -> dict:
    """
    Query Google Safe Browsing to determine whether a URL is safe.

    Parameters:
        url (str): The URL to check, e.g. "http://example.com/page"

    Returns:
        dict: Safety status and threat details, or {"success": False, "error": "..."}.
    """
    # For this API the key goes in the query string, not the headers
    endpoint_with_key = f"{GSB_ENDPOINT}?key={GOOGLE_SAFE_BROWSING_API_KEY}"

    payload = {
        "client": {
            "clientId":      "email-threat-intel-poc",
            "clientVersion": "1.0.0",
        },
        "threatInfo": {
            "threatTypes": [
                "MALWARE",
                "SOCIAL_ENGINEERING",
                "UNWANTED_SOFTWARE",
                "POTENTIALLY_HARMFUL_APPLICATION",
            ],
            "platformTypes":    ["ANY_PLATFORM"],  # Check desktop and mobile threats
            "threatEntryTypes": ["URL"],
            "threatEntries":    [{"url": url}],
        },
    }

    try:
        response = requests.post(endpoint_with_key, json=payload, timeout=10)

        if response.status_code == 200:
            data = response.json()

            # An empty response body {} means Google found no threats — URL is safe
            if not data or "matches" not in data:
                return {
                    "success":           True,
                    "url":               url,
                    "is_safe":           True,
                    "threat_type":       None,
                    "threat_description": None,
                }

            # If matches exist, URL is malicious — take the first (most severe) match
            threat_code = data["matches"][0].get("threatType", "THREAT_TYPE_UNSPECIFIED")
            return {
                "success":           True,
                "url":               url,
                "is_safe":           False,
                "threat_type":       threat_code,
                "threat_description": THREAT_DESCRIPTIONS.get(threat_code, threat_code),
            }

        elif response.status_code == 400:
            return {"success": False, "error": "Bad request to Google Safe Browsing. Check the URL format."}
        elif response.status_code == 403:
            return {"success": False, "error": "Invalid Google Safe Browsing API key. Check your .env file."}
        elif response.status_code == 429:
            return {"success": False, "error": "Google Safe Browsing rate limit reached. Wait and try again."}
        else:
            return {"success": False, "error": f"Google Safe Browsing returned HTTP {response.status_code}"}

    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Cannot reach Google Safe Browsing. Check your internet connection."}
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Google Safe Browsing request timed out after 10 seconds."}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


# ================================================================
# SECTION 4 — RISK SCORING
# Combines both API results into a single score from 0 to 100.
#
# Points breakdown:
#   AbuseIPDB confidence score × 0.60   → up to 60 points
#   High report count (50+)             → +10 bonus points
#   Google: malware or phishing         → +40 points
#   Google: other threat type           → +25 points
#   IP is whitelisted                   → -10 points
#
# Score → Verdict:
#   0–25   Low Risk
#   26–50  Medium Risk
#   51–75  High Risk
#   76–100 Critical Risk
# ================================================================

HIGH_SEVERITY_THREATS = {"MALWARE", "SOCIAL_ENGINEERING"}


def calculate_risk(ip_result: dict, url_result: dict) -> dict:
    """
    Combine AbuseIPDB and Google Safe Browsing results into a risk score.

    Parameters:
        ip_result  (dict): Output from check_ip()
        url_result (dict): Output from check_url()

    Returns:
        dict: {"score": int, "verdict": str, "factors": list[str]}
    """
    score   = 0
    factors = []   # Human-readable list of what drove the score

    # ── AbuseIPDB contribution (up to 60 points) ──────────────
    if ip_result.get("success"):
        abuse_score   = ip_result.get("abuse_confidence_score", 0)
        total_reports = ip_result.get("total_reports", 0)

        score += int(abuse_score * 0.60)   # Scale 0–100 down to 0–60

        if abuse_score > 0:
            factors.append(
                f"IP has an AbuseIPDB confidence score of {abuse_score}% "
                f"({total_reports} report(s))"
            )
        if total_reports >= 50:
            score = min(score + 10, 100)
            factors.append(f"High number of abuse reports ({total_reports})")
        if ip_result.get("is_whitelisted"):
            score = max(score - 10, 0)
            factors.append("IP is on the AbuseIPDB whitelist (trusted source)")
    else:
        factors.append("AbuseIPDB lookup failed — IP risk is unknown")

    # ── Google Safe Browsing contribution (up to 40 points) ───
    if url_result.get("success"):
        if not url_result.get("is_safe"):
            threat_type = url_result.get("threat_type", "")
            url_points  = 40 if threat_type in HIGH_SEVERITY_THREATS else 25
            score = min(score + url_points, 100)
            factors.append(
                f"URL flagged by Google Safe Browsing: "
                f"{url_result.get('threat_description', threat_type)}"
            )
        else:
            factors.append("URL passed Google Safe Browsing check (no threats detected)")
    else:
        factors.append("Google Safe Browsing lookup failed — URL risk is unknown")

    # ── Map score to a verdict label ───────────────────────────
    if score <= 25:   verdict = "Low Risk"
    elif score <= 50: verdict = "Medium Risk"
    elif score <= 75: verdict = "High Risk"
    else:             verdict = "Critical Risk"

    return {"score": score, "verdict": verdict, "factors": factors}


# ================================================================
# SECTION 5 — DISPLAY / REPORT FORMATTING
# Colour helpers and formatted print functions for the terminal.
# ================================================================

def red(t):    return Fore.RED    + str(t) + Style.RESET_ALL
def yellow(t): return Fore.YELLOW + str(t) + Style.RESET_ALL
def green(t):  return Fore.GREEN  + str(t) + Style.RESET_ALL
def cyan(t):   return Fore.CYAN   + str(t) + Style.RESET_ALL
def bold(t):   return Style.BRIGHT + str(t) + Style.RESET_ALL


def colour_verdict(verdict: str) -> str:
    if verdict == "Low Risk":      return green(verdict)
    if verdict == "Medium Risk":   return yellow(verdict)
    if verdict == "High Risk":     return red(verdict)
    if verdict == "Critical Risk": return red(bold(verdict))
    return verdict


def colour_score(score: int) -> str:
    if score <= 25: return green(str(score))
    if score <= 50: return yellow(str(score))
    return red(str(score))


def sep(char="─", width=60):
    print(char * width)


def print_ip_section(result: dict):
    print()
    print(bold(cyan("  [ AbuseIPDB — Sender IP Analysis ]")))
    sep()

    if not result.get("success"):
        print(f"  {red('ERROR:')} {result.get('error')}")
        return

    score_val = result["abuse_confidence_score"]
    score_str = (
        green(f"{score_val}%")  if score_val <= 25 else
        yellow(f"{score_val}%") if score_val <= 60 else
        red(f"{score_val}%")
    )

    print(f"  {'IP Address':<22} {result['ip']}")
    print(f"  {'Abuse Confidence':<22} {score_str}")
    print(f"  {'Total Reports':<22} {result['total_reports']}")
    print(f"  {'ISP':<22} {result['isp']}")
    print(f"  {'Domain':<22} {result['domain']}")
    print(f"  {'Country':<22} {result['country']}")
    print(f"  {'Usage Type':<22} {result['usage_type']}")
    print(f"  {'Last Reported':<22} {result['last_reported'] or 'Never'}")
    if result.get("is_whitelisted"):
        print(f"  {'Whitelisted':<22} {green('Yes (trusted source)')}")


def print_url_section(result: dict):
    print()
    print(bold(cyan("  [ Google Safe Browsing — URL Analysis ]")))
    sep()

    if not result.get("success"):
        print(f"  {red('ERROR:')} {result.get('error')}")
        return

    print(f"  {'URL':<22} {result['url']}")
    if result["is_safe"]:
        print(f"  {'Status':<22} {green('SAFE')} — no threats detected")
        print(f"  {'Threat Type':<22} N/A")
    else:
        print(f"  {'Status':<22} {red('MALICIOUS')} — URL is flagged!")
        print(f"  {'Threat Type':<22} {red(result['threat_type'])}")
        print(f"  {'Details':<22} {result['threat_description']}")


def print_risk_section(risk: dict):
    print()
    print(bold(cyan("  [ Risk Assessment ]")))
    sep()
    print(f"  {'Final Score':<22} {colour_score(risk['score'])}/100")
    print(f"  {'Verdict':<22} {colour_verdict(risk['verdict'])}")
    print()
    print("  Contributing Factors:")
    for factor in risk["factors"]:
        print(f"    • {factor}")


def print_report(ip: str, url: str, ip_result: dict, url_result: dict, risk: dict):
    print()
    sep("═")
    print(bold("        EMAIL THREAT INTELLIGENCE REPORT"))
    print(bold("        Sheridan College — Capstone PoC"))
    sep("═")
    print()
    print(f"  Sender IP  : {bold(ip)}")
    print(f"  URL        : {bold(url)}")
    print_ip_section(ip_result)
    print_url_section(url_result)
    print_risk_section(risk)
    print()
    sep("═")
    print(
        f"  Final Risk Score: {colour_score(risk['score'])}/100  |  "
        f"Verdict: {colour_verdict(risk['verdict'])}"
    )
    sep("═")
    print()


# ================================================================
# SECTION 6 — ENTRY POINT
# Collects user input, runs the queries, and prints the report.
# ================================================================

def get_input(prompt: str) -> str:
    """Prompt the user for input. Repeats if empty. Exits on Ctrl+C."""
    try:
        value = input(prompt).strip()
        if not value:
            print(yellow("  Input cannot be empty. Please try again."))
            return get_input(prompt)
        return value
    except KeyboardInterrupt:
        print("\n\n  Exiting. Goodbye!")
        raise SystemExit(0)


def main():
    print()
    print(bold(cyan("  Email Threat Intelligence Aggregator — PoC")))
    print(bold(cyan("  Sheridan College Capstone")))
    sep()
    print("  Enter the details below. Press Ctrl+C at any time to quit.")
    print()

    ip_address = get_input("  Sender IP address  : ")
    url        = get_input("  URL from email     : ")

    print()
    print(cyan("  Querying AbuseIPDB ..."))
    ip_result = check_ip(ip_address)

    print(cyan("  Querying Google Safe Browsing ..."))
    url_result = check_url(url)

    print(cyan("  Calculating risk score ..."))
    risk = calculate_risk(ip_result, url_result)

    print_report(ip_address, url, ip_result, url_result, risk)


if __name__ == "__main__":
    main()