#!/usr/bin/env python3
"""Pull original and layered variants and record durations.

Example:
    python step10_pull_benchmark_layers.py --input-path images.csv --n 10 \
        --source-prefix registry.example.com/source --push-prefix registry.example.com/variants \
        --output results.csv

Scenario output paths default to filenames in the current working directory.
--output overrides the selected scenario path without adding the _sizes suffix.
"""

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Dict, Set, Optional

GREEN = "\033[92m"
BLUE = "\033[94m"
RESET = "\033[0m"

def load_json(path: Path) -> List[Any]:
    """Load images from a CSV file with index or id, organization, image_name, tag."""
    data = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int(row.get("index") or row["id"])
            except (KeyError, ValueError):
                idx = 0
            data.append({
                "index": idx,
                "organization": row.get("organization", ""),
                "image_name": row.get("image_name", ""),
                "tag": row.get("tag", "") or "latest",
            })
    return data

def build_variant_name(image_name: str, push_prefix: str | None, source_prefix: str) -> str:
    prefix = source_prefix
    if image_name.startswith(prefix):
        remainder = image_name[len(prefix) :]
    else:
        remainder = image_name

    remainder = remainder.strip("/")
    if not remainder:
        return image_name

    if push_prefix:
        return f"{push_prefix}/{remainder}"
    return remainder

def pull_and_time(image: str) -> float:
    start = time.monotonic()
    subprocess.run(["docker", "pull", image], check=True)
    end = time.monotonic()
    return end - start


def remove_image(image: str) -> None:
    subprocess.run(["docker", "image", "rm", "-f", image], check=False)



def get_completed_images(csv_path: Path) -> Set[int]:
    """Read the CSV file and return a set of completed image indices."""
    completed = set()
    if not csv_path.exists():
        return completed
    
    try:
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader is None:
                return completed
            for row in reader:
                if row.get("iteration") == "avg":
                    try:
                        index = int(row.get("index", 0))
                        completed.add(index)
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    
    return completed


def append_rows_to_csv(rows: List[List[Any]], output_path: Path) -> None:
    """Append rows to the CSV file."""
    with output_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    print(f"{BLUE}✓ Saved {len(rows)} rows to {output_path}{RESET}")


def parse_index_range(value: str) -> Set[int]:
    """Parse index range like '3-50' into a set of ints (inclusive)."""
    value = value.strip()
    if not value:
        return set()
    if "-" not in value:
        raise ValueError("Index range must be in the form start-end (e.g., 3-50)")
    start_str, end_str = value.split("-", 1)
    start = int(start_str.strip())
    end = int(end_str.strip())
    if start > end:
        start, end = end, start
    return set(range(start, end + 1))


def parse_index_list(value: str) -> Set[int]:
    """Parse index list like '1,4,6,20' into a set of ints."""
    value = value.strip()
    if not value:
        return set()
    items = [v.strip() for v in value.split(",") if v.strip()]
    return {int(v) for v in items}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-path", type=Path, required=True, help="Input image CSV")
    parser.add_argument("--source-prefix", default="", help="Registry/path for original images")
    parser.add_argument("--push-prefix", default="", help="Registry/path for layered variants")
    parser.add_argument(
        "--output-path-standard", type=Path, default=Path("pull_benchmark_results.csv"),
        help="Standard scenario base output path (adds _sizes before .csv)",
    )
    parser.add_argument(
        "--output-path-adaptive", type=Path, default=Path("pull_benchmark_results_adaptive.csv"),
        help="Adaptive scenario base output path (adds _sizes before .csv)",
    )
    parser.add_argument(
        "--output-path-adaptive-flattened", type=Path,
        default=Path("pull_benchmark_results_adaptive_flattened.csv"),
        help="Flattened scenario base output path (adds _sizes before .csv)",
    )
    parser.add_argument(
        "--output-path-adaptive-flattened-skewed", type=Path,
        default=Path("pull_benchmark_results_adaptive_flattened_skewed_1.csv"),
        help="Skewed scenario base output path (adds _sizes before .csv)",
    )
    parser.add_argument("--n", type=int, help="Number of images (used if no index filters are set)")
    parser.add_argument(
        "--index-range",
        type=str,
        help="Index interval to include (e.g., 3-50)",
    )
    parser.add_argument(
        "--index-list",
        type=str,
        help="Comma-separated list of indices to include (e.g., 1,4,6,20)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of iterations per image",
    )
    parser.add_argument(
        "--scenario",
        choices=["standard", "adaptive"],
        default="standard",
        help="standard: benchmark original vs 250mb-layers and 500mb-layers; adaptive: benchmark original vs adaptive-layers",
    )
    parser.add_argument(
        "--flattened",
        action="store_true",
        help="Benchmark original vs adaptive-layers vs flattened (implies adaptive scenario)",
    )
    parser.add_argument(
        "--skewed", "--unbalanced-layers",
        action="store_true",
        help="Benchmark all four variants, adding skewed-layers (implies adaptive and flattened)",
    )
    parser.add_argument("--output", type=Path, help="Results CSV; defaults to a separate *_sizes.csv file")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild variant tags instead of reusing existing registry images")

    args = parser.parse_args()
    source_prefix = args.source_prefix.strip().rstrip("/")
    source_prefix = f"{source_prefix}/" if source_prefix else ""
    push_prefix = args.push_prefix.strip().rstrip("/")
    if args.skewed:
        args.flattened = True

    index_filter: Set[int] = set()
    if args.index_range:
        index_filter.update(parse_index_range(args.index_range))
    if args.index_list:
        index_filter.update(parse_index_list(args.index_list))

    if not index_filter and not args.n:
        raise ValueError("Provide --n or at least one of --index-range/--index-list")

    if index_filter:
        print(
            f"{GREEN}Building layered images for selected indices ({len(index_filter)} total)...{RESET}"
        )
    else:
        print(
            f"{GREEN}Building layered images for {args.n} image(s)...{RESET}"
        )

    data = load_json(args.input_path)
    if index_filter:
        images = [item for item in data if isinstance(item, dict) and item.get("index") in index_filter]
    else:
        images = data[: args.n]

    if args.n and index_filter:
        images = images[: args.n]

    adaptive = args.scenario == "adaptive" or args.flattened
    OUTPUT_PATH = args.output_path_adaptive if adaptive else args.output_path_standard
    if args.flattened:
        OUTPUT_PATH = args.output_path_adaptive_flattened
    if args.skewed:
        OUTPUT_PATH = args.output_path_adaptive_flattened_skewed
    OUTPUT_PATH = args.output or OUTPUT_PATH.with_name(OUTPUT_PATH.stem + "_sizes.csv")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Define CSV header
    if adaptive:
        csv_header = [
            "organization", "image_name", "tag", "index", "iteration",
            "original", "adaptive-layers",
        ]
    else:
        csv_header = [
            "organization", "image_name", "tag", "index", "iteration",
            "original", "250mb-layers", "500mb-layers",
        ]
    if args.flattened:
        csv_header.append("flattened")
    if args.skewed:
        csv_header.append("skewed-layers")

    
    # Initialize CSV file with header if it doesn't exist
    if not OUTPUT_PATH.exists():
        with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(csv_header)
    else:
        with OUTPUT_PATH.open("r", encoding="utf-8", newline="") as f:
            if next(csv.reader(f), None) != csv_header:
                raise ValueError(f"CSV header does not match the requested scenario: {OUTPUT_PATH}")
    
    # Check for completed images and print resume status
    completed_indices = get_completed_images(OUTPUT_PATH)
    images_to_process = [
        (idx, item) for idx, item in enumerate(images, 1)
        if item.get("index") not in completed_indices
    ]
    
    if completed_indices:
        print(f"{GREEN}Found existing results. Resuming from checkpoint...{RESET}")
        print(f"{GREEN}Completed images: {sorted(completed_indices)}{RESET}")
    
    if images_to_process:
        print(f"{GREEN}Processing {len(images_to_process)} remaining image(s)...{RESET}")
    else:
        total_count = args.n if args.n else len(images)
        print(f"{GREEN}All {total_count} image(s) already processed!{RESET}")
        return
    
    rows_buffer: List[List[Any]] = []
    
    for image_idx, item in images_to_process:
        if not isinstance(item, dict):
            continue
        organization = item.get("organization", "")
        image_name_orig = item.get("image_name", "")
        tag = item.get("tag") or "latest"
        index = item.get("index", "")
        
        if not image_name_orig:
            continue

        print(f"{GREEN}Processing image {image_idx}/{args.n}...{RESET}")

        step11_args = [
            sys.executable,
            "step11_build_layered_images.py",
            "--image-name",
            f"{image_name_orig}:{tag}",
            "--source-prefix",
            source_prefix,
            "--push-prefix",
            push_prefix,
        ]
        if not args.rebuild:
            step11_args.append("--check-exists")
        if adaptive:
            step11_args.append("--adaptive")
        if args.flattened:
            step11_args.append("--flattened")
        if args.skewed:
            step11_args.append("--skewed")

        build_result = subprocess.run(step11_args, cwd=Path(__file__).parent)
        if build_result.returncode != 0:
            print(
                f"{GREEN}Error building layered images for {image_name_orig}:{tag} — skipping benchmark.{RESET}"
            )
            # Write a skipped "avg" row so this image is not retried on resume
            image_name_skip = image_name_orig.lower().replace(
                "ghcr.io/", source_prefix
            )
            skip_row = [organization, image_name_skip, tag, index, "avg"] + ["error"] * (len(csv_header) - 5)
            append_rows_to_csv([skip_row], OUTPUT_PATH)
            continue

        image_name = image_name_orig.lower().replace(
            "ghcr.io/", source_prefix
        )

        original = f"{image_name}:{tag}"
        variant_250 = f"{build_variant_name(image_name, push_prefix, source_prefix)}:250mb-layers"
        variant_500 = f"{build_variant_name(image_name, push_prefix, source_prefix)}:500mb-layers"
        variant_adaptive = f"{build_variant_name(image_name, push_prefix, source_prefix)}:adaptive-layers"

        if adaptive:
            variants_to_benchmark = [(original, "original"), (variant_adaptive, "adaptive-layers")]
        else:
            variants_to_benchmark = [
                (original, "original"),
                (variant_250, "250mb-layers"),
                (variant_500, "500mb-layers"),
            ]
        if args.flattened:
            variant_flattened = f"{build_variant_name(image_name, push_prefix, source_prefix)}:flattened"
            variants_to_benchmark.append((variant_flattened, "flattened"))
        if args.skewed:
            variant_skewed = f"{build_variant_name(image_name, push_prefix, source_prefix)}:skewed-layers"
            variants_to_benchmark.append((variant_skewed, "skewed-layers"))

        all_durations: dict = {key: [] for _, key in variants_to_benchmark}

        for iteration in range(1, args.iterations + 1):
            durations = {key: "" for _, key in variants_to_benchmark}

            for image_variant, key in variants_to_benchmark:
                try:
                    duration = pull_and_time(image_variant)
                    durations[key] = f"{duration:.3f}"
                    all_durations[key].append(duration)
                except subprocess.CalledProcessError:
                    pass
                finally:
                    remove_image(image_variant)
                    time.sleep(3)

            rows_buffer.append(
                [organization, image_name, tag, index, iteration]
                + [durations[key] for _, key in variants_to_benchmark]

            )

            # Write after each iteration
            append_rows_to_csv(rows_buffer, OUTPUT_PATH)
            rows_buffer = []

        # Calculate averages from collected iterations (no additional pull needed)
        avg_row = [organization, image_name, tag, index, "avg"]
        for _, key in variants_to_benchmark:
            lst = all_durations[key]
            avg_row.append(f"{sum(lst)/len(lst):.3f}" if lst else "")

        rows_buffer.append(avg_row)
        append_rows_to_csv(rows_buffer, OUTPUT_PATH)
        rows_buffer = []

    print(
        f"{GREEN}Benchmark complete. Results saved to {OUTPUT_PATH}{RESET}"
    )


if __name__ == "__main__":
    main()
