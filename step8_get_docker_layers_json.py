
import subprocess
import json
import sys
import tarfile
import os
import csv
import copy

# Usage: python step8_get_docker_layers_json.py <input_file> --local-registry=HOST/PATH [--limit=N] [--index=START-END]

def get_layers_for_image(image_name, tag, local_registry):
    # Replace ghcr.io with local_registry
    patched_image_name = image_name.replace("ghcr.io", local_registry).lower()
    full_image = f"{patched_image_name}:{tag}"
    tar_path = "_tmp_image.tar"
    # Pull the image
    print(f"Pulling {full_image}...")
    try:
        result = subprocess.run(["docker", "pull", full_image], check=True, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print(f"Error: docker pull for {full_image} timed out after 10 minutes.")
        return {"pull_failed": True, "pull_error": "timeout after 10 minutes"}
    except subprocess.CalledProcessError as e:
        err_output = (e.stderr or "") + (e.stdout or "") if hasattr(e, 'stderr') else str(e)
        SKIP_ERRORS = [
            "unsupported media type application/vnd.buildkit.cacheconfig.v0",
            "mismatched image rootfs and manifest layers",
        ]
        if any(msg in err_output for msg in SKIP_ERRORS):
            print(f"Skipping image {full_image} due to incompatible image format: {err_output.strip()[:120]}")
            return None  # Special marker for unsupported/incompatible
        else:
            print(f"Error pulling {full_image}: {err_output}")
            # Return a special marker for pull failure, including error message
            return {"pull_failed": True, "pull_error": err_output}
    # Get total image size from Docker client
    try:
        inspect_out = subprocess.check_output(["docker", "image", "inspect", full_image, "--format", "{{.Size}}"])
        total_size_bytes = int(inspect_out.decode().strip())
        total_size_mb = total_size_bytes / (1024 * 1024)
    except Exception:
        total_size_bytes = None
        total_size_mb = None
    # Save the image as a tar file
    print(f"Saving {full_image} to {tar_path}...")
    subprocess.run(["docker", "save", "-o", tar_path, full_image], check=True)
    layers = []
    try:
        with tarfile.open(tar_path, "r") as tar:
            tar_files = {member.name: member for member in tar.getmembers()}
            # Docker layout
            if "manifest.json" in tar_files:
                manifest = json.load(tar.extractfile(tar_files["manifest.json"]))
                config_jsons = {}
                layer_tar_sizes = {}
                for name, member in tar_files.items():
                    if name.endswith(".json") and name != "manifest.json":
                        config_jsons[name] = tar.extractfile(member).read()
                    elif name.endswith("/layer.tar"):
                        layer_tar_sizes[name] = member.size
                config_filename = manifest[0]["Config"]
                config_json_bytes = None
                if config_filename in config_jsons:
                    config_json_bytes = config_jsons[config_filename]
                else:
                    config_basename = os.path.basename(config_filename)
                    for k, v in config_jsons.items():
                        if k.endswith(config_basename):
                            config_json_bytes = v
                            break
                if not config_json_bytes:
                    config_basename = os.path.basename(config_filename)
                    oci_config_path = f"blobs/sha256/{config_basename}"
                    if oci_config_path in tar_files:
                        config_json_bytes = tar.extractfile(tar_files[oci_config_path]).read()
                    else:
                        return []
                config = json.loads(config_json_bytes)
                if "rootfs" not in config or "diff_ids" not in config["rootfs"]:
                    return []
                layer_tar_paths = manifest[0]["Layers"]
                diff_ids = config["rootfs"]["diff_ids"]
                for layer_tar_path, digest in zip(layer_tar_paths, diff_ids):
                    size = layer_tar_sizes.get(layer_tar_path)
                    if size is None:
                        # Newer Docker/containerd format stores layers as blobs/sha256/<compressed_hash>
                        # instead of <hash>/layer.tar — look up the path directly
                        if layer_tar_path in tar_files:
                            size = tar_files[layer_tar_path].size
                    if size is None:
                        # Last resort: try diff_id as OCI blob path (uncompressed digest)
                        digest_short = digest.split(":", 1)[1]
                        oci_layer_path = f"blobs/sha256/{digest_short}"
                        if oci_layer_path in tar_files:
                            size = tar_files[oci_layer_path].size
                    if isinstance(size, int):
                        size_mb = size / (1024 * 1024)
                    else:
                        size_mb = None
                    layers.append({"digest": digest, "size_bytes": size, "size_mb": size_mb})
            elif "index.json" in tar_files:
                index = json.load(tar.extractfile(tar_files["index.json"]))
                manifest_desc = None
                for m in index.get("manifests", []):
                    if m.get("mediaType", "").endswith("manifest.v1+json"):
                        manifest_desc = m
                        break
                if not manifest_desc:
                    return []
                manifest_digest = manifest_desc["digest"].replace(":", "/")
                manifest_path = f"blobs/{manifest_digest}"
                if manifest_path not in tar_files:
                    return []
                manifest = json.load(tar.extractfile(tar_files[manifest_path]))
                # Check for unsupported media types in layers
                unsupported = False
                for layer in manifest.get("layers", []):
                    if layer.get("mediaType", "").startswith("application/vnd.buildkit.cacheconfig"):
                        unsupported = True
                        break
                if unsupported:
                    print(f"Skipping image {full_image} due to unsupported media type in layers.")
                    return None  # Special marker for unsupported
                config_digest = manifest["config"]["digest"].split(":", 1)[1]
                config_path = f"blobs/sha256/{config_digest}"
                if config_path not in tar_files:
                    found = False
                    for k in tar_files.keys():
                        if k.endswith(config_digest):
                            config_path = k
                            found = True
                            break
                    if not found:
                        return []
                config = json.load(tar.extractfile(tar_files[config_path]))
                if "rootfs" not in config or "diff_ids" not in config["rootfs"]:
                    return []
                for layer in manifest["layers"]:
                    digest = layer["digest"]
                    size = layer["size"]
                    size_mb = size / (1024 * 1024) if isinstance(size, int) else None
                    layers.append({"digest": digest, "size_bytes": size, "size_mb": size_mb})
            else:
                return []
    except Exception as e:
        if "unsupported media type application/vnd.buildkit.cacheconfig.v0" in str(e):
            print(f"Skipping image {full_image} due to unsupported media type error.")
            return None  # Special marker for unsupported
        else:
            print(f"Error processing {full_image}: {e}")
            return []
    if os.path.exists(tar_path):
        os.remove(tar_path)
    # Do not remove the image from Docker
    return layers

limit = None
index_start = None
index_end = None
input_file = None
local_registry = None

if len(sys.argv) < 2:
    print("Usage: python step8_get_docker_layers_json.py <input_file> --local-registry=HOST/PATH [--limit=N] [--index=START-END]")
    sys.exit(1)

input_file = sys.argv[1]

extra_args = sys.argv[2:]
i = 0
while i < len(extra_args):
    arg = extra_args[i]
    if arg.startswith("--local-registry="):
        local_registry = arg.split("=", 1)[1]
    elif arg == "--local-registry":
        if i + 1 >= len(extra_args) or extra_args[i + 1].startswith("--"):
            print("--local-registry requires a registry value, e.g. registry.example.com/mirror.")
            sys.exit(1)
        local_registry = extra_args[i + 1]
        i += 1
    elif arg.startswith("--limit="):
        try:
            limit = int(arg.split("=", 1)[1])
        except Exception:
            print("Invalid limit value. Use --limit=N where N is an integer.")
            sys.exit(1)
    elif arg == "--limit":
        if i + 1 >= len(extra_args):
            print("--limit requires a value.")
            sys.exit(1)
        try:
            limit = int(extra_args[i + 1])
            i += 1
        except Exception:
            print("Invalid limit value. Use --limit N where N is an integer.")
            sys.exit(1)
    elif arg.startswith("--index="):
        try:
            parts = arg.split("=", 1)[1].split("-")
            index_start = int(parts[0])
            index_end = int(parts[1])
        except Exception:
            print("Invalid index range. Use --index=START-END where START and END are integers.")
            sys.exit(1)
    elif arg == "--index":
        if i + 1 >= len(extra_args):
            print("--index requires a range value, e.g. --index 20-50.")
            sys.exit(1)
        try:
            parts = extra_args[i + 1].split("-")
            index_start = int(parts[0])
            index_end = int(parts[1])
            i += 1
        except Exception:
            print("Invalid index range. Use --index START-END where START and END are integers.")
            sys.exit(1)
    else:
        print(f"Unknown argument: {arg}")
        print("Usage: python step8_get_docker_layers_json.py <input_file> --local-registry=HOST/PATH [--limit=N] [--index=START-END]")
        sys.exit(1)
    i += 1

if local_registry is None:
    print("--local-registry is required.")
    sys.exit(1)
local_registry = local_registry.strip().rstrip("/")
if not local_registry or "://" in local_registry or any(char.isspace() for char in local_registry):
    print("Invalid --local-registry. Use HOST/PATH without a URL scheme or whitespace.")
    sys.exit(1)

# Load input file — supports JSON or CSV
if input_file.endswith(".csv"):
    images = []
    with open(input_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entry = {
                "index": int(row["index"]),
                "image_name": row["image_name"],
                "tag": row["tag"],
            }
            if "organization" in row:
                entry["organization"] = row["organization"]
            images.append(entry)
else:
    with open(input_file, "r", encoding="utf-8") as f:
        images = json.load(f)
    # Assign positional index if not present
    for i, img in enumerate(images):
        if "index" not in img:
            img["index"] = i + 1

# Apply --index range filter
if index_start is not None and index_end is not None:
    images = [img for img in images if index_start <= img.get("index", 0) <= index_end]
    print(f"Filtered to index range {index_start}-{index_end}: {len(images)} image(s)")

if limit is not None:
    images = images[:limit]

input_json_file = input_file


output_json_file = input_json_file + ".layers.json"
print(f"Output file: {os.path.abspath(output_json_file)}")
# Load already processed images if output file exists
already_done = set()
all_entries = []
max_id = 0
if os.path.exists(output_json_file):
    with open(output_json_file, "r", encoding="utf-8") as f:
        try:
            all_entries = json.load(f)
            print(f"Resuming: loaded {len(all_entries)} already-processed entries from output file.")
            for e in all_entries:
                k = (e.get("image_name"), e.get("tag"))
                already_done.add(k)
                if "id" in e and isinstance(e["id"], int):
                    max_id = max(max_id, e["id"])
        except Exception as ex:
            print(f"[WARN] Could not load existing output file: {ex}")


def color_text(text, color_code):
    return f"\033[{color_code}m{text}\033[0m"

DEBUG_COLOR = "36"  # Cyan
INFO_COLOR = "32"   # Green
WARN_COLOR = "33"   # Yellow
ERR_COLOR = "31"    # Red

print(color_text(f"Already done: {len(already_done)} image(s). Remaining to process: {sum(1 for img in images if (img.get('image_name'), img.get('tag')) not in already_done)}", INFO_COLOR))
next_id = max_id + 1


def flush_output():
    with open(output_json_file, "w", encoding="utf-8") as f:
        json.dump(all_entries, f, indent=2)
        f.write("\n")
    print(color_text(f"[INFO] Output file updated: {len(all_entries)} total entries saved.", INFO_COLOR))


for idx, entry in enumerate(images):
    k = (entry.get("image_name"), entry.get("tag"))
    if k in already_done:
        print(color_text(f"Skipping already processed: {k}", INFO_COLOR))
        continue
    # Only process if not already done
    entry_out = copy.deepcopy(entry)
    image_name = entry["image_name"]
    tag = entry["tag"]
    # Check for size in entry (input JSON must have size info)
    max_size_bytes = 5 * 1024 * 1024 * 1024  # 5GB
    input_size_bytes = None
    input_size_mb = None
    # Try common keys for size
    for key in ["size_bytes", "docker_total_size_bytes", "total_size_bytes"]:
        if key in entry and isinstance(entry[key], (int, float)):
            input_size_bytes = entry[key]
            input_size_mb = input_size_bytes / (1024 * 1024)
            break
    if input_size_bytes is not None and input_size_bytes > max_size_bytes:
        print(color_text(f"Skipping image {k} due to size > 5GB ({input_size_mb:.2f} MB)", WARN_COLOR))
        entry_out["skipped_due_to_size"] = True
        entry_out["layers"] = []
        entry_out["docker_total_size_bytes"] = input_size_bytes
        entry_out["docker_total_size_mb"] = input_size_mb
        new_entries.append(entry_out)
        continue
    # Only run Docker operations for new images
    print(color_text(f"({idx+1}/{len(images)}) Processing: {k}", INFO_COLOR))
    layers = get_layers_for_image(image_name, tag, local_registry)
    entry_out["id"] = next_id
    next_id += 1
    if isinstance(layers, dict) and layers.get("pull_failed"):
        print(color_text(f"[ERROR] Docker pull failed for {k}: {layers.get('pull_error')}", ERR_COLOR))
        entry_out["layers"] = []
        entry_out["pull_failed"] = True
        entry_out["pull_error"] = layers.get("pull_error")
        entry_out["docker_total_size_bytes"] = None
        entry_out["docker_total_size_mb"] = None
        all_entries.append(entry_out)
        already_done.add(k)
        flush_output()
        continue
    if layers is None:
        entry_out["layers"] = []
    else:
        entry_out["layers"] = layers
    try:
        patched_image_name = image_name.replace("ghcr.io", local_registry).lower()
        full_image = f"{patched_image_name}:{tag}"
        inspect_out = subprocess.check_output(["docker", "image", "inspect", full_image, "--format", "{{.Size}}"])
        total_size_bytes = int(inspect_out.decode().strip())
        total_size_mb = total_size_bytes / (1024 * 1024)
    except Exception:
        total_size_bytes = None
        total_size_mb = None
    entry_out["docker_total_size_bytes"] = total_size_bytes
    entry_out["docker_total_size_mb"] = total_size_mb
    all_entries.append(entry_out)
    already_done.add(k)
    # Remove the image from Docker to free up space
    try:
        subprocess.run(["docker", "image", "rm", full_image], check=False, capture_output=True)
        print(color_text(f"[INFO] Removed image {full_image} from Docker to free up space.", INFO_COLOR))
    except Exception as e:
        print(color_text(f"[WARN] Failed to remove image {full_image}: {e}", WARN_COLOR))
    flush_output()

print(color_text(f"[INFO] Done. {len(all_entries)} total entries in {output_json_file}", INFO_COLOR))
