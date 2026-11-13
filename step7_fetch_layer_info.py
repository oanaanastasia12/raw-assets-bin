"""Fetch layer info using --input-file images.json --output-file image_layers.json."""

import argparse
import os
from getpass import getpass
from pathlib import Path

import requests
import json

# Settings
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input-file", type=Path, required=True, help="Input image dataset JSON file")
parser.add_argument("--output-file", type=Path, required=True, help="Output JSON file with layer information")
args = parser.parse_args()
input_file = args.input_file
output_file = args.output_file

# github token with read:packages scope
GITHUB_PAT = os.environ.get("GITHUB_PAT") or getpass("GitHub PAT: ")

# Read input images
with open(input_file, "r", encoding="utf-8") as f:
    images = json.load(f)

results = []

for img in images:
    image_name = img["image_name"]  # e.g., ghcr.io/OWNER/IMAGE
    tag = img["tag"]
    # Parse owner/repo from image_name (supporting subpaths)
    if not image_name.startswith("ghcr.io/"):
        print(f"Invalid image_name: {image_name}")
        continue
    parts = image_name.replace("ghcr.io/", "").split("/", 1)
    if len(parts) != 2:
        print(f"Invalid image_name: {image_name}")
        continue
    owner, repo = parts
    manifest_url = f"https://ghcr.io/v2/{owner}/{repo}/manifests/{tag}"
    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
    }
    try:
        resp = requests.get(manifest_url, headers=headers)
        resp.raise_for_status()
        manifest = resp.json()
        layers = [
            {"digest": l.get("digest"), "size": l.get("size")}
            for l in manifest.get("layers", [])
        ]
        img_result = img.copy()
        img_result["layers"] = layers
        results.append(img_result)
        print(f"Fetched layers for {image_name}:{tag}")
    except Exception as e:
        print(f"Failed to fetch {image_name}:{tag}: {e}")

# Write results to output file
with open(output_file, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
print(f"Results written to {output_file}")
