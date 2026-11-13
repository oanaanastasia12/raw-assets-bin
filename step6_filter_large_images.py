"""Filter large images using --input-file image_sizes.json --output-file large_images.json."""

import argparse
import json
from pathlib import Path

SIZE_THRESHOLD = 500 * 1024 * 1024  # 500MB in bytes

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", type=Path, required=True, help="Input image dataset JSON file")
    parser.add_argument("--output-file", type=Path, required=True, help="Output filtered image dataset JSON file")
    args = parser.parse_args()

    with open(args.input_file, "r") as f:
        data = json.load(f)

    # Filter images with valid size_bytes >= 500MB
    filtered = []
    for img in data:
        size_bytes = img.get("size_bytes")
        if isinstance(size_bytes, int) and size_bytes >= SIZE_THRESHOLD:
            img = dict(img)  #copy
            img["size_megabytes"] = round(size_bytes / (1024 * 1024), 5)
            filtered.append(img)

    with open(args.output_file, "w") as f:
        json.dump(filtered, f, indent=2)

    print(f"Filtered {len(filtered)} images >= 500MB. Saved to {args.output_file}.")

if __name__ == "__main__":
    main()
