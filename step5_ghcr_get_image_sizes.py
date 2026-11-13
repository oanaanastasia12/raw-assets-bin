"""Fetch image sizes using --input-file, --output-file, and --output-failures."""

import argparse
import json
import requests
import os
from getpass import getpass
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", type=Path, required=True, help="Input image dataset JSON file")
    parser.add_argument("--output-file", type=Path, required=True, help="Output dataset JSON file with image sizes")
    parser.add_argument("--output-failures", type=Path, required=True, help="Output failures JSON file")
    args = parser.parse_args()

GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME") or input("GitHub username: ")
GITHUB_PAT = os.environ.get("GITHUB_PAT") or getpass("GitHub PAT: ")

HEADERS_LIST = [
    {"Accept": "application/vnd.oci.image.manifest.v1+json"},
    {"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
]


def get_registry_token(org, image):
    scope = f"repository:{org}/{image}:pull"
    token_url = f"https://ghcr.io/token?service=ghcr.io&scope={scope}"
    resp = requests.get(token_url, auth=(GITHUB_USERNAME, GITHUB_PAT))
    if resp.status_code == 200:
        return resp.json().get("token")
    else:
        print(
            f"Failed to get registry token for {org}/{image}: {resp.status_code} {resp.text}"
        )
        return None


def get_tags(org, image, token):
    url = f"https://ghcr.io/v2/{org}/{image}/tags/list"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        return resp.json().get("tags", [])
    else:
        print(f"Failed to get tags for {org}/{image}: {resp.status_code} {resp.text}")
        return []


def get_image_size(org, image, tag, token):
    for headers in HEADERS_LIST:
        headers = dict(headers)
        headers["Authorization"] = f"Bearer {token}"
        url = f"https://ghcr.io/v2/{org}/{image}/manifests/{tag}"
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            manifest = resp.json()
            # Handle manifest list (multi-arch)
            if manifest.get("mediaType", "").endswith("manifest.list.v2+json"):
                for m in manifest.get("manifests", []):
                    plat = m.get("platform", {})
                    # Try all platforms, prefer amd64/linux
                    if plat.get("architecture") == "amd64" and plat.get("os") == "linux":
                        digest = m["digest"]
                        url2 = f"https://ghcr.io/v2/{org}/{image}/manifests/{digest}"
                        resp2 = requests.get(url2, headers=headers)
                        if resp2.status_code == 200:
                            manifest2 = resp2.json()
                            return sum(
                                layer.get("size", 0) for layer in manifest2.get("layers", [])
                            )
                # If no amd64/linux, try first available
                if manifest.get("manifests"):
                    digest = manifest["manifests"][0]["digest"]
                    url2 = f"https://ghcr.io/v2/{org}/{image}/manifests/{digest}"
                    resp2 = requests.get(url2, headers=headers)
                    if resp2.status_code == 200:
                        manifest2 = resp2.json()
                        return sum(
                            layer.get("size", 0) for layer in manifest2.get("layers", [])
                        )
                return None
            elif "layers" in manifest:
                return sum(layer.get("size", 0) for layer in manifest["layers"])
    return None


def main(input_file, output_file, output_failures):
    with open(input_file) as f:
        images = json.load(f)
    failures = []
    with open(output_file, "w") as out:
        out.write("[\n")
        first = True
        for idx, entry in enumerate(images):
            try:
                image_name = entry["image_name"]
                tag = "latest"
                if ":" in image_name:
                    image_name, tag = image_name.rsplit(":", 1)
                parts = image_name.replace("ghcr.io/", "").split("/")
                org = parts[0]
                image = "/".join(parts[1:])
                token = get_registry_token(org, image)
                if not token:
                    entry["size_bytes"] = None
                    failures.append({"image": image_name, "reason": "token"})
                    print(f"{image_name}:{tag} -> token unavailable")
                else:
                    tags = get_tags(org, image, token)
                    found = False
                    for t in [tag] + [tg for tg in tags if tg != tag]:
                        size = get_image_size(org, image, t, token)
                        if size is not None:
                            entry["size_bytes"] = size
                            entry["tag"] = t
                            print(f"{image_name}:{t} -> {size / (1024*1024):.2f} MB")
                            found = True
                            break
                    if not found:
                        entry["size_bytes"] = None
                        failures.append({"image": image_name, "reason": "no valid tag", "tags": tags})
                        print(f"{image_name}:{tag} -> size unavailable (tried tags: {[tag] + tags}")
                # Write entry immediately
                if not first:
                    out.write(",\n")
                out.write(json.dumps(entry, indent=2))
                first = False
            except Exception as e:
                entry["size_bytes"] = None
                failures.append({"image": entry.get("image_name", "?"), "reason": str(e)})
                print(f"{entry.get('image_name', '?')} -> error: {e}")
                if not first:
                    out.write(",\n")
                out.write(json.dumps(entry, indent=2))
                first = False
        out.write("\n]")
    with open(output_failures, "w") as fail:
        json.dump(failures, fail, indent=2)
    print(
        f"Saved updated dataset to {output_file} and failures to {output_failures}"
    )


if __name__ == "__main__":
    main(args.input_file, args.output_file, args.output_failures)
