"""Extract GHCR image names using --input-file organizations.json --output-file images.txt."""

import argparse
import json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", type=Path, required=True, help="Input organizations JSON file")
    parser.add_argument("--output-file", type=Path, required=True, help="Output image names text file")
    args = parser.parse_args()

    with open(args.input_file) as f:
        data = json.load(f)

    image_names = []
    for org in data:
        org_name = org["organization"]
        for pkg in org.get("packages", []):
            # Replace '/' in package name with '-' for GHCR compatibility
            image = f"ghcr.io/{org_name}/{pkg['name'].replace('/', '-')}"
            image_names.append(image)

    # Print to stdout
    for img in image_names:
        print(img)

    # Write to file
    with open(args.output_file, "w") as out:
        for img in image_names:
            out.write(img + "\n")
    print(f"\nSaved {len(image_names)} image names to {args.output_file}")

if __name__ == "__main__":
    main()
