"""
cli.py — Command-line entry point.

    python -m cairn.cli --dataset-urn "urn:li:dataset:(...)"
"""

from __future__ import annotations

import click
from dotenv import load_dotenv

from .agent import main as run_agent


@click.command()
@click.option(
    "--dataset-urn",
    required=True,
    help="The DataHub URN of the dataset Cairn should inspect.",
)
def cli(dataset_urn: str) -> None:
    load_dotenv()
    run_agent(dataset_urn)


if __name__ == "__main__":
    cli()
