#!/usr/bin/env python3
"""Filter images with non-null layer sizes and add a sequential index.

Usage: python step9_filter_images_with_layers.py --input-path layers.json --output-path indexed.json
"""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-path", type=Path, required=True, help="Input image layers JSON or JSONL file")
    parser.add_argument("--output-path", type=Path, required=True, help="Output filtered and indexed JSON file")
    args = parser.parse_args()

    with args.input_path.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            f.seek(0)
            data = [json.loads(line) for line in f if line.strip()]

    if not isinstance(data, list):
        raise ValueError("Expected top-level JSON array of images")

    filtered = []
    index = 1
    total = len(data)
    with_layers = 0
    empty_layers = 0
    invalid_layer_sizes = 0

    for item in data:
        layers = item.get("layers") if isinstance(item, dict) else None
        if isinstance(layers, list):
            with_layers += 1
            if len(layers) == 0:
                empty_layers += 1
                continue

            if all(
                isinstance(layer, dict) and layer.get("size_bytes") is not None
                for layer in layers
            ):
                new_item = dict(item)
                new_item["index"] = index
                filtered.append(new_item)
                index += 1
            else:
                invalid_layer_sizes += 1

    with args.output_path.open("w", encoding="utf-8") as f:
        json.dump(filtered, f, indent=2)

    print(
        "Summary: total=%d, with_layers=%d, empty_layers=%d, invalid_layer_sizes=%d, written=%d"
        % (total, with_layers, empty_layers, invalid_layer_sizes, len(filtered))
    )
    print(f"Wrote {len(filtered)} images to {args.output_path}")


if __name__ == "__main__":
    main()
