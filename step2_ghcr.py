"""Scan GHCR organizations: python step2_ghcr.py --output-file results.json."""

import argparse
import os
import requests
import json
import time
from datetime import datetime
from getpass import getpass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-file",
        required=True,
        help="Path to the JSON file where scan results will be saved",
    )
    args = parser.parse_args()

# --- Configuration ---
GITHUB_API_BASE = "https://api.github.com"
GITHUB_PAT = os.environ.get("GITHUB_PAT") or getpass("GitHub PAT: ")

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
}
if GITHUB_PAT:
    HEADERS["Authorization"] = f"token {GITHUB_PAT}"
else:
    print("WARNING: GITHUB_PAT environment variable not set. API rate limits will be severely restricted (60 requests/hour).")
    print("Please create a PAT with 'read:org' and 'read:packages' scopes for better performance.")

# Configure proxies from environment variables
PROXIES = {
    'http': os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY'),
    'https': os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY'),
}
# Remove None values
PROXIES = {k: v for k, v in PROXIES.items() if v is not None}
if PROXIES:
    print(f"Using proxy configuration: {PROXIES}")
else:
    PROXIES = None

# --- Load Organizations from filtered_orgs.json ---
print("Loading organizations from filtered_orgs.json...")
with open("filtered_orgs.json", "r") as f:
    filtered_orgs_data = json.load(f)
    ORGANIZATIONS_TO_CHECK = [org["login"] for org in filtered_orgs_data]
print(f"Loaded {len(ORGANIZATIONS_TO_CHECK)} organizations from filtered_orgs.json")

# --- API Interaction Functions ---

def get_org_packages(org_name, package_type="container", per_page=30):
    """
    Fetches a list of packages for a given organization and package type.
    """
    packages = []
    page = 1
    
    print(f"  Checking packages for {org_name}...")
    
    while True:
        url = f"{GITHUB_API_BASE}/orgs/{org_name}/packages"
        params = {
            "package_type": package_type,
            "page": page,
            "per_page": per_page
        }
        
        try:
            response = requests.get(url, headers=HEADERS, params=params, proxies=PROXIES, timeout=30)
            response.raise_for_status() # Raise an exception for HTTP errors
            
            data = response.json()
            if not data: # No more packages or end of results
                break
            
            packages.extend(data)
            
            # Check for Link header for pagination (GitHub API standard)
            if 'link' in response.headers:
                next_page_link = None
                links = response.headers['link'].split(', ')
                for link in links:
                    if 'rel="next"' in link:
                        next_page_link = link.split(';')[0].strip('<>')
                        break
                if next_page_link:
                    page += 1
                    time.sleep(0.5) # Be kind to the API
                else:
                    break
            else:
                break # No link header, assume single page or end of results

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # print(f"    Organization '{org_name}' not found or no access to packages.")
                return [] # Organization might not exist or no public packages
            elif e.response.status_code == 403:
                # Check if it's a rate limit issue
                if 'X-RateLimit-Remaining' in e.response.headers and int(e.response.headers['X-RateLimit-Remaining']) == 0:
                    reset_time = int(e.response.headers['X-RateLimit-Reset'])
                    wait_time = max(0, reset_time - int(time.time()) + 5) # Add 5 seconds buffer
                    print(f"    Rate limit exceeded for {org_name}. Waiting for {wait_time} seconds until {datetime.fromtimestamp(reset_time).strftime('%Y-%m-%d %H:%M:%S')}.")
                    time.sleep(wait_time)
                    # Retry the same page after waiting
                    continue
                else:
                    # 403 but not rate limit - likely missing read:packages scope or private org
                    if page == 1:
                        print(f"    Access forbidden for {org_name} (check token has 'read:packages' scope)")
                    return []
            else:
                print(f"    Error fetching packages for {org_name}: {e}")
                return [] # Other HTTP error
        except requests.exceptions.RequestException as e:
            print(f"    Network error for {org_name}: {e}")
            return [] # General request error
            
    return packages

# --- Main Script Logic ---

def find_orgs_with_ghcr_images(org_list, output_file):
    """
    Iterates through a list of organizations and identifies those with GHCR container images.
    """
    found_organizations = []
    
    print(f"Starting scan for organizations publishing Docker images to GHCR ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}).")
    print(f"Checking {len(org_list)} organizations.")

    for i, org_name in enumerate(org_list):
        print(f"Processing ({i+1}/{len(org_list)}): {org_name}")
        packages = get_org_packages(org_name, package_type="container")
        
        if packages:
            org_info = {
                "organization": org_name,
                "num_container_packages": len(packages),
                "packages": [
                    {
                        "name": pkg.get("name"),
                        "visibility": pkg.get("visibility"),
                        "url": pkg.get("html_url")
                    }
                    for pkg in packages
                ]
            }
            found_organizations.append(org_info)
            print(f"  --> Found {len(packages)} container packages for {org_name}!")
        else:
            print(f"  No public container packages found for {org_name}.")
        
        time.sleep(0.2) # Small delay between organizations to be polite and avoid bursts

    with open(output_file, 'w') as f:
        json.dump(found_organizations, f, indent=4)
        
    print(f"\n--- Scan Complete ---")
    print(f"Found {len(found_organizations)} organizations with public GHCR images.")
    print(f"Results saved to '{output_file}'.")

# --- Execute the script ---
if __name__ == "__main__":
    find_orgs_with_ghcr_images(ORGANIZATIONS_TO_CHECK, args.output_file)