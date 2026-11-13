#!/usr/bin/env python3
"""Build and push standard, adaptive, flattened, and 80/20 skewed image variants.

Example:
    python step11_build_layered_images.py --image-name ghcr.io/org/image:latest \
        --source-prefix registry.example.com/source \
        --push-prefix registry.example.com/variants --base-output ./layered_build \
        --adaptive
"""

import argparse
import csv
import math
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, List, Tuple

SUFFIX_250 = "250mb-layers"
SUFFIX_500 = "500mb-layers"
SUFFIX_ADAPTIVE = "adaptive-layers"
SUFFIX_FLATTENED = "flattened"
SUFFIX_SKEWED = "skewed-layers"
TARGET_250_MB = 250 * 1024 * 1024
TARGET_500_MB = 500 * 1024 * 1024

def get_adaptive_layer_count(total_size_bytes: int) -> int:
    """Return number of equal layers based on image size."""
    gb2 = 2 * 1024 ** 3
    gb5 = 5 * 1024 ** 3
    if total_size_bytes < gb2:
        return 5
    elif total_size_bytes <= gb5:
        return 10
    else:
        return 15

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

def export_image_fs(image: str, export_tar: Path, rootfs_dir: Path) -> None:
    container_id = (
        subprocess.check_output(["docker", "create", image]).decode("utf-8").strip()
    )
    try:
        subprocess.run(["docker", "export", "-o", str(export_tar), container_id], check=True)
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], check=False)

    rootfs_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(export_tar, "r") as tar:
        tar.extractall(path=rootfs_dir, filter="fully_trusted")

def get_all_files(root: Path) -> List[Tuple[str, int]]:
    # Temporarily make everything readable so we can access all files
    subprocess.run(["chmod", "-R", "u+r", str(root)], check=False)
    
    files: List[Tuple[str, int]] = []
    for dirpath, _, filenames in os.walk(root):
        directory_links = [name for name in os.listdir(dirpath)
                           if (Path(dirpath) / name).is_symlink() and name not in filenames]
        for name in filenames + directory_links:
            full_path = Path(dirpath) / name
            rel_path = str(full_path.relative_to(root))
            try:
                file_stat = full_path.lstat()
            except (PermissionError, OSError):
                # Skip files we can't access (permission denied, etc.)
                continue
            if stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
                files.append((rel_path, file_stat.st_size))
            elif (
                stat.S_ISSOCK(file_stat.st_mode)
                or stat.S_ISFIFO(file_stat.st_mode)
                or stat.S_ISCHR(file_stat.st_mode)
                or stat.S_ISBLK(file_stat.st_mode)
            ):
                continue
            else:
                raise RuntimeError(f"Unknown filesystem entry: {rel_path}")
    return files

def group_files_by_target_size(
    files: Iterable[Tuple[str, int]], target_size: float, max_groups: int
) -> List[List[Tuple[str, int]]]:
    groups: List[List[Tuple[str, int]]] = []
    current_group: List[Tuple[str, int]] = []
    current_size = 0.0

    for rel_path, size in sorted(files):
        if (
            current_group
            and current_size + size > target_size
            and len(groups) < max_groups - 1
        ):
            groups.append(current_group)
            current_group = []
            current_size = 0.0
        current_group.append((rel_path, size))
        current_size += size

    if current_group:
        groups.append(current_group)

    while len(groups) < max_groups:
        groups.append([])

    return groups

def group_files_by_weights(files: Iterable[Tuple[str, int]], weights: List[float]) -> List[List[Tuple[str, int]]]:
    ordered = sorted(files)
    if len(ordered) < len(weights):
        raise RuntimeError(f"Cannot create {len(weights)} non-empty layers from {len(ordered)} files")
    total_size = sum(size for _, size in ordered)
    groups = []
    position = 0
    cumulative_size = 0
    cumulative_weight = 0.0
    for index, weight in enumerate(weights[:-1]):
        cumulative_weight += weight
        target = total_size * cumulative_weight
        group = [ordered[position]]
        cumulative_size += ordered[position][1]
        position += 1
        reserve = len(weights) - index - 1
        while position < len(ordered) - reserve:
            next_size = ordered[position][1]
            if abs(cumulative_size + next_size - target) > abs(cumulative_size - target):
                break
            group.append(ordered[position])
            cumulative_size += next_size
            position += 1
        groups.append(group)
    groups.append(ordered[position:])
    return groups

def materialize_layers(rootfs_dir: Path, context_dir: Path, groups: List[List[Tuple[str, int]]]) -> List[str]:
    created_dirs: List[str] = []
    for i, group in enumerate(groups):
        if not group:
            continue
        layer_dir = context_dir / f"layer_{i}"
        layer_dir.mkdir(parents=True, exist_ok=True)
        directories = {Path(".")}
        if i == 0:
            for dirpath, dirnames, _ in os.walk(rootfs_dir):
                for name in dirnames:
                    source_dir = Path(dirpath) / name
                    if not source_dir.is_symlink():
                        relative_dir = source_dir.relative_to(rootfs_dir)
                        (layer_dir / relative_dir).mkdir(parents=True, exist_ok=True)
                        directories.add(relative_dir)
        has_files = False
        for rel_path, _ in group:
            src = rootfs_dir / rel_path
            dst = layer_dir / rel_path
            src_stat = src.lstat()
            dst.parent.mkdir(parents=True, exist_ok=True)
            directories.update(Path(rel_path).parents)
            if stat.S_ISLNK(src_stat.st_mode):
                os.symlink(os.readlink(src), dst)
            else:
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
            has_files = True
        # for relative_dir in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        #     shutil.copystat(rootfs_dir / relative_dir, layer_dir / relative_dir)
        if has_files:
            created_dirs.append(f"layer_{i}")
    return created_dirs

def write_dockerfile(context_dir: Path, created_dirs: List[str]) -> None:
    dockerfile_path = context_dir / "Dockerfile"
    with dockerfile_path.open("w", encoding="utf-8") as f:
        f.write("FROM scratch\n")
        for layer_dir in created_dirs:
            f.write(f"COPY {layer_dir}/ /\n")

def build_and_push(context_dir: Path, image: str, expected_layer_count: int | None = None, push: bool = True) -> None:
    subprocess.run(["docker", "build", "-t", image, str(context_dir)], check=True)
    if push:
        subprocess.run(["docker", "push", image], check=True)

def prune_builder_cache() -> None:
    subprocess.run(["docker", "builder", "prune", "-f"], check=False)

def remove_image(image: str) -> None:
    subprocess.run(["docker", "image", "rm", "-f", image], check=False)

def image_exists_remote(image: str) -> bool:
    result = subprocess.run(
        ["docker", "manifest", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

def get_image_sizes(image: str) -> Tuple[int, int]:
    """Get uncompressed and compressed sizes of an image. Returns (uncompressed, compressed)."""
    # Try to pull if not available locally, but don't fail if it doesn't exist remotely
    result = subprocess.run(["docker", "pull", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        # Get uncompressed size
        uncompressed = int(
            subprocess.check_output(["docker", "inspect", image, "--format={{.Size}}"]).decode().strip()
        )
    except (subprocess.CalledProcessError, ValueError):
        uncompressed = 0
    
    try:
        # Get compressed size using docker save
        result = subprocess.check_output(f"docker save {image} 2>/dev/null | wc -c", shell=True).decode().strip()
        compressed = int(result)
    except (subprocess.CalledProcessError, ValueError):
        compressed = 0
    
    return (uncompressed, compressed)

def build_copy_variant(rootfs_dir: Path, variant_image: str, base_output: Path,
                       groups: List[List[Tuple[str, int]]], push: bool = True) -> None:
    with tempfile.TemporaryDirectory(dir=base_output) as temp_dir:
        context_dir = Path(temp_dir)
        created_dirs = materialize_layers(rootfs_dir, context_dir, groups)
        if len(created_dirs) != len(groups):
            raise RuntimeError("Partition contains empty layers")
        write_dockerfile(context_dir, created_dirs)
        try:
            # print(f"Context kept at: {context_dir}")        #new
            # input("Press Enter to continue...")             #new
            build_and_push(context_dir, variant_image, expected_layer_count=len(groups), push=push)
        finally:
            prune_builder_cache()

def build_flattened_variant(rootfs_dir: Path, variant_image: str, base_output: Path, push: bool = True) -> None:
    files = get_all_files(rootfs_dir)
    if not files:
        raise RuntimeError("No files found in exported root filesystem")
    build_copy_variant(rootfs_dir, variant_image, base_output, [files], push=push)

def build_skewed_variant(rootfs_dir: Path, variant_image: str, base_output: Path, push: bool = True) -> None:
    files = get_all_files(rootfs_dir)
    total_size = sum(size for _, size in files)
    if not files or total_size <= 0:
        raise RuntimeError("No file content found in exported root filesystem")
    layer_count = get_adaptive_layer_count(total_size)
    weights = [0.8] + [0.2 / (layer_count - 1)] * (layer_count - 1)
    groups = group_files_by_weights(files, weights)
    print("Skewed layer byte shares:", [round(100 * sum(size for _, size in group) / total_size, 2)
                                        for group in groups])
    build_copy_variant(rootfs_dir, variant_image, base_output, groups, push=push)

def build_layered_variant(
    rootfs_dir: Path,
    variant_image: str,
    target_size_bytes: int,
    base_output: Path,
    layer_count: int | None = None,
    push: bool = True,
) -> None:
    files = get_all_files(rootfs_dir)
    total_size = sum(size for _, size in files)
    if total_size == 0 or not files:
        raise RuntimeError("No files found in exported root filesystem")
    if layer_count is not None:
        groups = group_files_by_weights(files, [1 / layer_count] * layer_count)
    else:
        layer_count = max(1, math.ceil(total_size / target_size_bytes))
        groups = group_files_by_target_size(files, total_size / layer_count, layer_count)
        groups = [group for group in groups if group]
    build_copy_variant(rootfs_dir, variant_image, base_output, groups, push=push)

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--push-prefix", required=True, help="Registry/path for variants; use an empty string for no prefix")
    parser.add_argument("--source-prefix", default="", help="Registry/path replacing ghcr.io; empty uses unqualified source names")
    parser.add_argument(
        "--base-output", type=Path, default=Path("layered_build"),
        help="Parent directory for temporary build files (default: ./layered_build)",
    )
    parser.add_argument(
        "--image-name",
        type=str,
        required=True,
        help="Image name with optional tag (e.g., ghcr.io/F-WuTS/compapi21:v1.0)",
    )
    parser.add_argument(
        "--check-exists",
        action="store_true",
        help="Skip build/push if the target image tag already exists in local registry",
    )
    parser.add_argument(
        "--adaptive",
        action="store_true",
        help="Build a single adaptive-layers variant (5/10/15 equal layers based on image size) instead of 250mb/500mb variants",
    )
    parser.add_argument(
        "--flattened",
        action="store_true",
        help="Build a single-layer flattened variant; combine with --adaptive to build both",
    )
    parser.add_argument(
        "--skewed", "--unbalanced-layers",
        action="store_true",
        help="Build skewed-layers with 80%% in one layer and 20%% across the others; uses adaptive layer count",
    )
    parser.add_argument(
        "--index",
        type=int,
        help="Image index (for use with --get-sizes)",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Do not push the built image to registry",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not remove the variant image from the local Docker host after building",
    )
    parser.add_argument(
        "--get-sizes",
        type=str,
        help="Extract original and adaptive variant sizes and save to CSV file (requires --index)",
    )
    args = parser.parse_args()

    push_prefix = args.push_prefix.strip().rstrip("/")
    source_prefix = args.source_prefix.strip().rstrip("/")
    source_prefix = f"{source_prefix}/" if source_prefix else ""
    base_output = args.base_output
    base_output.mkdir(parents=True, exist_ok=True)

    # Parse image name and tag from the provided string
    if ":" in args.image_name:
        image_name, tag = args.image_name.rsplit(":", 1)
    else:
        image_name = args.image_name
        tag = "latest"

    image_name_lower = image_name.lower().replace(
        "ghcr.io/", source_prefix
    )

    variant_name = build_variant_name(image_name_lower, push_prefix, source_prefix)

    flattened_image = f"{variant_name}:{SUFFIX_FLATTENED}" if args.flattened else None
    if flattened_image and args.check_exists and image_exists_remote(flattened_image):
        flattened_image = None
    skewed_image = f"{variant_name}:{SUFFIX_SKEWED}" if args.skewed else None
    if skewed_image and args.check_exists and image_exists_remote(skewed_image):
        skewed_image = None

    variants_to_build = []
    if args.adaptive:
        # Adaptive mode: one variant with equal layers, count based on image size
        variant_image = f"{variant_name}:{SUFFIX_ADAPTIVE}"
        # Skip check_exists if --get-sizes is set (need to ensure variant exists for size extraction)
        should_skip = args.check_exists and not args.get_sizes and image_exists_remote(variant_image)
        if not should_skip:
            variants_to_build = [(variant_image, None)]
    elif not args.flattened and not args.skewed:
        # Standard mode: 250mb and 500mb variants
        variants_to_build = []
        for suffix, target_size_bytes in ((SUFFIX_250, TARGET_250_MB), (SUFFIX_500, TARGET_500_MB)):
            variant_image = f"{variant_name}:{suffix}"
            # Skip check_exists if --get-sizes is set (need to ensure variant exists for size extraction)
            should_skip = args.check_exists and not args.get_sizes and image_exists_remote(variant_image)
            if should_skip:
                continue
            variants_to_build.append((variant_image, target_size_bytes))

    if not variants_to_build and flattened_image is None and skewed_image is None:
        return

    # Pull and export the source image once, reuse rootfs for all variants
    with tempfile.TemporaryDirectory(dir=base_output) as run_temp:
        run_output = Path(run_temp)
        with tempfile.TemporaryDirectory(dir=run_output) as shared_temp:
            shared_path = Path(shared_temp)
            export_tar = shared_path / "rootfs.tar"
            rootfs_dir = shared_path / "rootfs"

            subprocess.run(["docker", "pull", f"{image_name_lower}:{tag}"], check=True)
            export_image_fs(f"{image_name_lower}:{tag}", export_tar, rootfs_dir)
            remove_image(f"{image_name_lower}:{tag}")

            for root, dirs, files in os.walk(rootfs_dir):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        mode = os.stat(path).st_mode
                        os.chmod(path, mode | stat.S_IRUSR)
                    except Exception:
                        pass

            export_tar.unlink(missing_ok=True)  # free space after extraction
            
            built_images = []
            original_sizes = (0, 0)  # Will store (uncompressed, compressed) for original image
            
            # If --get-sizes, extract original image size before removing it
            if args.get_sizes:
                print(f"Extracting original image size...")
                original_image = f"{image_name_lower}:{tag}"
                original_sizes = get_image_sizes(original_image)
            
            # Now remove the original image (it was already exported/extracted)
            remove_image(f"{image_name_lower}:{tag}")
            
            # Build adaptive variant if needed (for --get-sizes or regular build)
            if args.adaptive and variants_to_build:
                # Compute layer count from actual extracted size
                files = get_all_files(rootfs_dir)
                total_size = sum(size for _, size in files)
                layer_count = get_adaptive_layer_count(total_size)
                target_size_bytes = max(1, total_size // layer_count)
                print(f"Adaptive mode: {total_size/(1024**3):.2f} GB → {layer_count} equal layers")
                variant_image, _ = variants_to_build[0]
                build_layered_variant(rootfs_dir, variant_image, target_size_bytes, run_output, layer_count=layer_count, push=not args.no_push)
                built_images.append(variant_image)
            
            # Handle --get-sizes if requested (after building the variant)
            if args.get_sizes:
                if args.index is None:
                    raise ValueError("--get-sizes requires --index")
                
                adaptive_image = f"{variant_name}:{SUFFIX_ADAPTIVE}"
                
                # Get adaptive variant size
                print(f"Extracting adaptive variant size...")
                adapt_uncomp, adapt_comp = get_image_sizes(adaptive_image)
                
                # Write to CSV using previously extracted original sizes
                output_csv = Path(args.get_sizes)
                output_csv.parent.mkdir(parents=True, exist_ok=True)
                
                # Check if file exists and has header
                file_exists = output_csv.exists()
                with open(output_csv, "a" if file_exists else "w") as f:
                    writer = csv.writer(f)
                    if not file_exists:
                        writer.writerow([
                            "index", "image_name",
                            "original_uncompressed", "original_compressed",
                            "adaptive_uncompressed", "adaptive_compressed"
                        ])
                    writer.writerow([
                        args.index, f"{image_name}:{tag}",
                        original_sizes[0], original_sizes[1],
                        adapt_uncomp, adapt_comp
                    ])
                print(f"Sizes saved to {output_csv}")
                
                # Clean up and return after saving sizes
                if not args.no_clean:
                    for img in built_images:
                        remove_image(img)
                return
            
            if flattened_image is not None:
                build_flattened_variant(rootfs_dir, flattened_image, run_output, push=not args.no_push)
                built_images.append(flattened_image)
            if skewed_image is not None:
                build_skewed_variant(rootfs_dir, skewed_image, run_output, push=not args.no_push)
                built_images.append(skewed_image)

            if not args.adaptive and variants_to_build:
                for variant_image, target_size_bytes in variants_to_build:
                    build_layered_variant(rootfs_dir, variant_image, target_size_bytes, run_output, push=not args.no_push)
                    built_images.append(variant_image)
            
            # Remove images if --no-clean is not set
            if not args.no_clean:
                for img in built_images:
                    remove_image(img)
if __name__ == "__main__":
    main()
