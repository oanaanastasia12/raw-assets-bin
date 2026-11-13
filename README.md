# Structure of this repository
## Dataset information:
`benchmark_image_list.csv`   --> contains the full dataset of publicly available container images at the time of collection
`image_metadata_dataset_750.csv`  --> contains the metadata for the full dataset: name, layer count, the original and balanced compressed sizes in bytes <br>
`image_metadata_subset_100.csv`  --> contains the metadata for the subset: name, the original, the balanced, the flattened and the skewed compressed sizes in bytes


## Experiment 1 results for full dataset
`pull_benchmark_results_balanced.csv`  --> raw pull duration results in seconds for original vs balanced <br>
`median_avg_perf_per_image.csv`  --> mean, median pull times, improvement percent for mean and median, and standard deviation for original and balanced


## Experiment 2 results for subset
`pull_benchmark_results_balanced_flattened_skewed.csv`  --> raw pull duration results in seconds for original vs balanced vs flattened vs skewed <br>
`median_avg_perf_per_image_subset.csv`  --> mean, median pull times, improvement percent for mean and median, and standard deviation for original, balanced, flattened, skewed

## Scripts
The scripts are numbered to be executed in order. <br>
The scripts representing steps from 1 to 9 are used to collect the dataset elements. <br>
The scripts for steps 10 and 11 are used for creating the new image variations and running the container image pull test iterations.


# Instructions for scripts

# Running the scripts

Run from this directory with Python 3 and `requests` installed. Set `GITHUB_PAT` (with `read:org` and `read:packages`) and `GITHUB_USERNAME` in your environment. Steps 8 onward require Docker and registry access.

## Steps 1-7

```bash
python3 step1_github_org_filter.py --countries Germany France "United Kingdom" --years 2023 2024 2025 --min-followers 20 --min-public-repos 5
python3 step2_ghcr.py --output-file organizations.json
python3 step3_ghcr_extract_image_names.py --input-file organizations.json --output-file image_names.txt
python3 step4_ghcr_build_image_dataset.py --input-file organizations.json --output-file images.json
python3 step5_ghcr_get_image_sizes.py --input-file images.json --output-file sizes.json --output-failures failures.json
python3 step6_filter_large_images.py --input-file sizes.json --output-file large_images.json
python3 step7_fetch_layer_info.py --input-file large_images.json --output-file image_layers.json
```

Step1 writes `filtered_orgs.json`, which step2 reads automatically. Step3's text list is a side output: step4 needs step2's JSON, not step3's text.

Step1 requires space-separated `--countries` and `--years` lists. Quote country names containing spaces. Minimum counts have the default values of 20 followers and 5 public repositories. Use a fresh working directory when changing the search scope to avoid resuming an old checkpoint.

## Steps 8-9

Replace the example registry with your GHCR mirror:

```bash
python3 step8_get_docker_layers_json.py large_images.json --local-registry registry.example.com/mirror
python3 step9_filter_images_with_layers.py --input-path large_images.json.layers.json --output-path indexed_images.json
```

## Step10

### Input csv example

benchmark_image_list.csv:
```csv
index,organization,image_name,tag
1,example,ghcr.io/example/image,latest
```

```bash
# Original vs adaptive
python3 step10_pull_benchmark_layers.py --input-path benchmark_image_list.csv --n 1 --iterations 10 --scenario adaptive \
  --source-prefix registry.example.com/mirror --push-prefix registry.example.com/local-repo \
  --output benchmark_adaptive.csv

# Original vs adaptive vs flattened
python3 step10_pull_benchmark_layers.py --input-path benchmark_image_list.csv --n 1 --iterations 10 --flattened \
  --source-prefix registry.example.com/mirror --push-prefix registry.example.com/local-repo \
  --output benchmark_adaptive_flattened.csv

# Original vs adaptive vs flattened vs skewed
python3 step10_pull_benchmark_layers.py --input-path benchmark_image_list.csv --n 1 --iterations 10 --skewed \
  --source-prefix registry.example.com/mirror --push-prefix registry.example.com/local-repo \
  --output benchmark_adaptive_flattened_skewed.csv
```

Every mode includes the original. `--flattened` implies adaptive; `--skewed` implies both adaptive and flattened. Step11 is called automatically to build missing variants.

- `--n N`: select the first N images in CSV order, not N iterations (`--iterations` controls repetitions).
- `--index-list 1,4,6`: select images by their CSV `index` values, keeping CSV order. No `--n` is needed.
- `--index-range 3-10`: select CSV indices 3 through 10, inclusive, keeping CSV order. No `--n` is needed.
- Together, `--index-list` filters first and `--n` limits the matching images to the first N. Already completed images are skipped, not replaced with later rows.

Example selecting indices 1, 4, and 6 (if present in the input CSV):

```bash
python3 step10_pull_benchmark_layers.py --input-path benchmark_image_list.csv --index-list 1,4,6 --iterations 10 --scenario adaptive \
  --source-prefix registry.example.com/mirror --push-prefix registry.example.com/local-repo \
  --output benchmark_selected.csv
```

Example selecting indices 3 through 10 (if present in the input CSV):

```bash
python3 step10_pull_benchmark_layers.py --input-path benchmark_image_list.csv --index-range 3-10 --iterations 10 --scenario adaptive \
  --source-prefix registry.example.com/mirror --push-prefix registry.example.com/local-repo \
  --output benchmark_range.csv
```

## Step11 separately

For indices 1-5 in `benchmark_image_list.csv` (same column order as above), replace both registry prefixes:

```bash
for i in 1 2 3 4 5; do
  image_name=$(grep "^$i," benchmark_image_list.csv | cut -d',' -f3,4 | sed 's/,/:/g; s/\r$//')
  echo "$i,$image_name"
  python3 step11_build_layered_images.py --image-name "$image_name" --index "$i" \
    --check-exists --adaptive --source-prefix registry.example.com/mirror \
    --push-prefix registry.example.com/local-repo
done
```

This builds and pushes missing adaptive variants without running the benchmark. The same can be done for other variants as well.
The adaptive variant corresponds to the balanced variant where layers sizes are balanced according to total image size, thus the output files will have adaptive printed instead of balanced.
