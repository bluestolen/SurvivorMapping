# Survivor Wilderness Map

Map terrain, water access, and wilderness resources.

## Setup

From this directory, create and activate the Conda environment:

```sh
conda env create --file environment.yml
conda activate survivor-wilderness-map
```

Conda resolves GDAL, GEOS, and PROJ from conda-forge alongside the pinned Python packages.

For a pip-only setup, use Python 3.12 in a separate virtual environment.
Install prebuilt wheels to avoid compiling geospatial libraries locally:

```sh
python -m venv .venv
# Activate .venv for your shell before installing.
python -m pip install --only-binary=:all: -r requirements.txt
```
