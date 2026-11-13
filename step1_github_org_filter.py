"""Find organizations using --countries, --years, and minimum follower/repository counts."""

import argparse
import requests
import time
import json
import os
from getpass import getpass

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--countries", nargs="+", required=True, help='Countries to search; quote names with spaces, e.g. "United Kingdom"')
parser.add_argument("--years", nargs="+", type=int, required=True, help="Creation years to search, e.g. 2023 2024 2025")
parser.add_argument("--min-followers", type=int, default=20, help="Minimum followers, inclusive (default: 20)")
parser.add_argument("--min-public-repos", type=int, default=5, help="Minimum public repositories, inclusive (default: 5)")
args = parser.parse_args()
if any(not country.strip() for country in args.countries):
    parser.error("--countries values must not be empty")
if any(year < 1 or year > 9999 for year in args.years):
    parser.error("--years values must be between 1 and 9999")
if args.min_followers < 0 or args.min_public_repos < 0:
    parser.error("Minimum follower and repository counts must be non-negative")
countries = [country.strip() for country in args.countries]
years = [f"{year:04d}" for year in args.years]

GITHUB_PAT = os.environ.get("GITHUB_PAT") or getpass("GitHub PAT: ")
# Settings
# Leave search_term empty to not filter by name
search_term = ""
min_followers = args.min_followers
min_public_repos = args.min_public_repos
token = GITHUB_PAT


headers = {"Authorization": f"token {token}"} if token else {}

def check_rate_limit():
    """Check remaining API rate limit"""
    resp = requests.get("https://api.github.com/rate_limit", headers=headers)
    data = resp.json()
    remaining = data['resources']['search']['remaining']
    reset_time = data['resources']['search']['reset']
    return remaining, reset_time

def wait_for_rate_limit():
    """Wait if rate limit is low"""
    remaining, reset_time = check_rate_limit()
    if remaining < 10:
        wait_seconds = reset_time - time.time() + 5
        print(f"Rate limit low ({remaining} remaining). Waiting {int(wait_seconds/60)} minutes...")
        time.sleep(wait_seconds)
    return remaining

# Search for organizations (up to 1000 per country/year, paginated).
orgs = []
per_page = 100
seen_orgs = set()  # Track unique orgs to avoid duplicates

# Load checkpoint if it exists
checkpoint_file = "filtered_orgs_checkpoint.json"
if os.path.exists(checkpoint_file):
    with open(checkpoint_file, "r") as f:
        checkpoint = json.load(f)
        orgs = checkpoint.get("orgs", [])
        seen_orgs = set(checkpoint.get("seen_orgs", []))
        print(f"Resuming from checkpoint: {len(orgs)} orgs found so far")

total_queries = 0
for country in countries:
    for year in years:
        for page in range(1, 11):  # GitHub Search API allows up to 1000 results (10 pages x 100)
            # Check rate limit every 10 queries
            if total_queries % 10 == 0:
                remaining = wait_for_rate_limit()
                print(f"Rate limit: {remaining} searches remaining")
            
            total_queries += 1
            query = f"type:org location:'{country}' created:{year}"
            if search_term:
                query += f" {search_term}"
            search_url = f"https://api.github.com/search/users?q={query}&per_page={per_page}&page={page}"
            response = requests.get(search_url, headers=headers).json()
            items = response.get("items", [])
            
            # Debug output
            if page == 1:
                print(f"{country} {year}: {len(items)} items, total_count={response.get('total_count', 'N/A')}")
                if "message" in response:
                    print(f"  ERROR: {response['message']}")
            
            # Add only unique orgs
            for item in items:
                if item["login"] not in seen_orgs:
                    orgs.append(item)
                    seen_orgs.add(item["login"])
            
            # Save checkpoint every 50 queries
            if total_queries % 50 == 0:
                with open(checkpoint_file, "w") as f:
                    json.dump({"orgs": orgs, "seen_orgs": list(seen_orgs)}, f)
                print(f"Checkpoint saved: {len(orgs)} unique orgs")
            
            time.sleep(0.5)  # Small delay between requests
            
            if len(items) < per_page:
                break

filtered_orgs = []


from datetime import datetime

print(f"\nFiltering {len(orgs)} organizations...")
for idx, org in enumerate(orgs):
    org_login = org["login"]
    
    # Check rate limit for core API
    if idx % 50 == 0:
        resp = requests.get("https://api.github.com/rate_limit", headers=headers).json()
        core_remaining = resp['resources']['core']['remaining']
        if core_remaining < 100:
            reset_time = resp['resources']['core']['reset']
            wait_seconds = reset_time - time.time() + 5
            print(f"Core rate limit low. Waiting {int(wait_seconds/60)} minutes...")
            time.sleep(wait_seconds)
        print(f"Processing org {idx+1}/{len(orgs)}, core API: {core_remaining} remaining")
    
    # Step 2: Get organization details
    org_url = f"https://api.github.com/orgs/{org_login}"
    org_data = requests.get(org_url, headers=headers).json()
    time.sleep(0.3)  # Small delay
    followers = org_data.get("followers", 0)
    public_repos = org_data.get("public_repos", 0)
    created_at = org_data.get("created_at", "1970-01-01T00:00:00Z")
    org_type = org_data.get("type", "")
    location = org_data.get("location", "")


    # Early filter: skip orgs that don't meet criteria
    if (
        followers < min_followers or
        public_repos < min_public_repos or
        org_type != "Organization"
    ):
        continue

    # Step 3: Get all repos and check stars (handle pagination)
    repos = []
    page = 1
    while True:
        repos_url = f"https://api.github.com/orgs/{org_login}/repos?per_page=100&page={page}"
        resp = requests.get(repos_url, headers=headers)
        time.sleep(0.3)  # Small delay
        page_repos = resp.json()
        if not isinstance(page_repos, list) or not page_repos:
            break
        repos.extend(page_repos)
        if len(page_repos) < 100:
            break
        page += 1
    if repos:
        filtered_orgs.append({
            "login": org_login,
            "url": org["html_url"],
            "followers": followers,
            "public_repos": public_repos,
            "created_at": created_at,
            "repo_count": len(repos)
        })

# Write results to a file
import json

# Clean up checkpoint file
if os.path.exists(checkpoint_file):
    os.remove(checkpoint_file)
    
with open("filtered_orgs.json", "w", encoding="utf-8") as f:
    json.dump(filtered_orgs, f, indent=2, ensure_ascii=False)
print(f"Results written to filtered_orgs.json ({len(filtered_orgs)} organizations)")
