"""Build a GHCR dataset using --input-file organizations.json --output-file dataset.json."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", type=Path, required=True, help="Input organizations JSON file")
    parser.add_argument("--output-file", type=Path, required=True, help="Output image dataset JSON file")
    args = parser.parse_args()

    with open(args.input_file) as f:
        orgs = json.load(f)

    dataset = []
    for org in orgs:
        org_name = org["organization"]
        for pkg in org.get("packages", []):
            dataset.append({
                "organization": org_name,
                "image_name": f"ghcr.io/{org_name}/{pkg['name'].replace('/', '-')}"
            })

    with open(args.output_file, "w") as out:
        json.dump(dataset, out, indent=2)

    print(f"Saved {len(dataset)} image entries to {args.output_file}")


if __name__ == "__main__":
    main()