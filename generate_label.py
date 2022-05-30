# standard library
import argparse
import csv, json
import os
import gzip
from re import U
import shutil
import hashlib

# third party
import pathlib
import requests

# project
from config import url_label_information, species_integrity
from utils import download_and_unzip, mkdir
from typing import Union
from config import chr_length, url_label_information

import typing


class Annotation_info(typing.NamedTuple):
    chromosome: str
    subtype: str
    start: int
    end: int


def download_families(families_path: Union[str, pathlib.Path]):
    """
    https://www.dfam.org/releases/Dfam_3.6/apidocs/#operation--families-get
    """
    base_url = "https://dfam.org/api/families"
    limit = 1000

    start = 1
    end = float("inf")

    print("downloading Dfam families...")

    families = {}
    while start < end:
        url = f"{base_url}?start={start}&limit={limit}"

        response = requests.get(url)
        response.raise_for_status()

        response_json = response.json()

        families_batch = response_json["results"]
        for family in families_batch:
            accession = family["accession"]
            assert accession not in families, f"duplicate accession ID {accession}"
            families[accession] = family

        print(f"{len(families)}")

        start += limit
        end = response_json["total_count"]

    print(f"{len(families)} total families downloaded")

    with open(families_path, "w") as json_file:
        json.dump(families, json_file)


def download_annotation(species: str):
    """all the annotated datasets generated from there to start.

    Args:
        species - the name of reference genome.
            e.g. hg38
    """
    folder = f"./annotation_label"
    mkdir(folder)
    checksum = species_integrity[f"{species}.hits"]
    download_and_unzip(
        species, folder, f"{species}.hits", url_label_information[species], checksum
    )  # checksum make sure the gz file integrity.
    families_filename = "families.json"
    families_path = pathlib.Path(families_filename)
    if not families_path.is_file():
        download_families(families_path)

    with open(families_path) as json_file:
        families = json.load(json_file)

    wanted = extract_lines(f"{folder}/{species}.hits", families)
    for chromosome, length in chr_length.items():
        data = list(filter(lambda x: x.chromosome == chromosome, wanted))
        save_annotations(folder, species, chromosome, data)


def extract_lines(file_name: str, families):
    """match the information of web with the hits files

    Args:
        file_name - whole path with species information.
            e.g. ./ref_datasets/hg38.fa
        families - the subtype of repeat sequence.
            e.g. LTR
    """
    print("Generate label datasets\U0001F43C\U0001F43E\U0001F43E")
    wanted = []
    with open(file_name, "r") as r:
        reader = csv.reader(r, delimiter="\t")
        for data in reader:
            accession = data[1]
            if not str(data[2]).startswith("LTR"):
                continue
            subtype = (families[accession]["classification"],)
            wanted.append(
                Annotation_info(
                    chromosome=data[0],
                    subtype=str(subtype),
                    start=data[9],
                    end=data[10],
                )
            )
    return wanted


def save_annotations(folder: str, species: str, chromosome: str, annotations: list):
    """make files to save the new datasets

    Args:
        folder - the generated path of files.
            e.g. ./ref_datasets
        species -  the name of reference genome.
            e.g. hg38
        chromosome - the chromosome of the chosen species.
            e.g. chr1
        annotations -  the target region, with its own alignment star, end and type.
    """

    annotations_csv = f"{folder}/{species}_{chromosome}.csv"
    with open(annotations_csv, "a+", newline="") as csv_file:
        csv_writer = csv.writer(csv_file, delimiter="\t", lineterminator="\n")
        csv_writer.writerows(
            [
                (data.chromosome, data.start, data.end, data.subtype)
                for data in annotations
            ]
        )
