"""
Custom Dataset, DataLoader, and supporting code.
"""


# standard library
import pathlib
import pickle

from typing import List, Type, Union

# third party
import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F

from pyfaidx import Fasta
from torch.utils.data.sampler import SubsetRandomSampler
from torchvision import transforms
from tqdm import tqdm

# project
from metadata import emojis
from utils import (
    CoordinatesToTensor,
    DnaSequenceMapper,
    SampleMapEncode,
    data_directory,
    genome_assemblies_directory,
)


class TranslateCoordinatesReverse:
    """Convert (center, span) relative coordinates to (start, end)."""

    def __init__(self):
        pass

    def __call__(self, target):
        # [n, 2]
        span, center = target[0], target[1]
        end = (span + 2 * center) / 2
        start = (span - 2 * center) / 2
        return (start, end)


class DeNormalizeCoordinates:
    """DeNormalize a sample's repeat annotation coordinates to a relative location
    in the sequence, defined as start and end floats between 0 and 1."""

    def __init__(self, segment_length):
        self.segment_length = segment_length

    def __call__(self, coordinates):
        return (
            int(coordinates[0].item() * self.segment_length),
            int(coordinates[1].item() * self.segment_length),
        )


class CategoryMapper:
    """
    Categorical data mapping class, with methods to translate from the category
    text labels to one-hot encoding and vice versa.
    """

    def __init__(self, categories):
        self.categories = sorted(categories)
        self.num_categories = len(self.categories)
        self.emojis = emojis[: self.num_categories + 2]
        self.label_to_index_dict = {
            label: index for index, label in enumerate(categories)
        }
        self.index_to_label_dict = {
            index: label for index, label in enumerate(categories)
        }
        self.index_to_emoji_dict = {
            index: emoji for index, emoji in enumerate(self.emojis)
        }

        self.label_to_emoji_dict = {
            label: self.index_to_emoji_dict[index]
            for index, label in enumerate(categories + ["sos", "eos"])
        }

    def label_to_index(self, label):
        """
        Get the class index of label.
        """
        return self.label_to_index_dict[label]

    def index_to_label(self, index):
        """
        Get the label string from its class index.
        """
        return self.index_to_label_dict[index]

    def label_to_emoji(self, index):
        return self.index_to_emoji_dict[index]

    def label_to_one_hot(self, label):
        """
        Get the one-hot representation of label.
        """
        one_hot_label = F.one_hot(
            torch.tensor(self.label_to_index_dict[label]),
            num_classes=self.num_categories,
        )
        one_hot_label = one_hot_label.type(torch.float32)
        return one_hot_label

    def one_hot_to_label(self, one_hot_label):
        """
        Get the label string from its one-hot representation.
        """
        index = torch.argmax(one_hot_label)
        label = self.index_to_label_dict[index]
        return label

    def print_label_and_emoji(self, logger):
        logger.info(self.label_to_emoji_dict)


class RepeatSequenceDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        genome_fasta_path: Union[str, pathlib.Path],
        annotation_path: Union[str, pathlib.Path],
        chromosomes: List[str],
        dna_sequence_mapper: Type[DnaSequenceMapper],
        segment_length: int = 2000,
        overlap: int = 500,
        transform=None,
    ):
        super().__init__()

        self.chromosomes = chromosomes
        self.dna_sequence_mapper = dna_sequence_mapper
        self.path = [
            f"{genome_fasta_path}/{chromosome}.fa" for chromosome in self.chromosomes
        ]
        self.annotation = [
            f"{annotations_path}/hg38_{chromosome}.csv"
            for chromosome in self.chromosomes
        ]

        self.segment_length = segment_length
        self.overlap = overlap
        self.transform = transform
        self.repeat_list = self.select_chr()

        repeat_types = self.get_unique_categories()
        print(len(repeat_types), repeat_types)
        self.repeat_type_mapper = CategoryMapper(repeat_types)

    def get_unique_categories(self):
        df_list = [
            pd.read_csv(annotation_path, sep="\t", names=["start", "end", "subtype"])
            for annotation_path in self.annotation
        ]
        return sorted(pd.concat(df_list)["subtype"].unique().tolist())

    def select_chr(self):
        repeat_list = []
        for fasta_path, chromosome, annotation_path in zip(
            self.path, self.chromosomes, self.annotation
        ):
            annotation_path = pathlib.Path(annotation_path)
            segments_repeats_pickle_path = (
                data_directory / annotation_path.name.replace(".csv", ".pickle")
            )

            # load the segments_with_repeats list from disk if it has already been generated
            if segments_repeats_pickle_path.is_file():
                with open(segments_repeats_pickle_path, "rb") as pickle_file:
                    segments_with_repeats = pickle.load(pickle_file)
            else:
                genome = Fasta(fasta_path)[chromosome]
                annotations = pd.read_csv(
                    annotation_path, sep="\t", names=["start", "end", "subtype"]
                )
                segments_with_repeats = self.get_segments_with_repeats(
                    genome, annotations
                )

                # save the segments_with_repeats list as a pickle file
                with open(segments_repeats_pickle_path, "wb") as pickle_file:
                    pickle.dump(segments_with_repeats, pickle_file)

            repeat_list.extend(segments_with_repeats)

        return repeat_list

    def get_the_corresponding_repeat(self, anno_df, start, end):
        repeats_in_sequence = anno_df.loc[
            (
                (anno_df["start"] >= start)
                & (anno_df["end"] <= end)
                & (anno_df["start"] < anno_df["end"])
            )
            # ----------------------------
            # ^seq_start                  ^seq_end
            #             -----------------------
            #             ^rep_start            ^rep_end
            | (
                (anno_df["start"] < end)
                & (end < anno_df["end"])
                & (anno_df["start"] < anno_df["end"])
            )
            #            ----------------------------
            #            ^seq_start                  ^seq_end
            # -----------------------
            # ^rep_start            ^rep_end
            | (
                (anno_df["start"] < start)
                & (start < anno_df["end"])
                & (anno_df["start"] < anno_df["end"])
            )
        ]
        return repeats_in_sequence

    def get_segments_with_repeats(self, genome, annotations):
        repeat_list = []
        for index in tqdm(range(len(genome) // (self.segment_length - self.overlap))):
            genome_index = index * (self.segment_length - self.overlap)
            anno_df = annotations
            start = genome_index
            end = genome_index + self.segment_length
            repeats_in_sequence = self.get_the_corresponding_repeat(anno_df, start, end)
            if not repeats_in_sequence.empty:
                repeats_in_sequence = repeats_in_sequence.apply(
                    lambda x: [
                        max(start, x["start"]),
                        min(end, x["end"]),
                        x["subtype"],
                    ],
                    axis=1,
                    result_type="broadcast",
                )
                repeat_list.append(
                    (genome[start:end].seq.upper(), start, repeats_in_sequence)
                )
        return repeat_list

    def forward_strand(self, index):
        sequence, start, repeats_in_sequence = self.repeat_list[index]
        # end = start + self.segment_length

        sample = {"sequence": sequence, "start": start}

        repeat_ids_series = repeats_in_sequence["subtype"].map(
            self.repeat_type_mapper.label_to_index
        )
        repeat_ids_array = np.array(repeat_ids_series, np.int32)
        repeat_ids_tensor = torch.tensor(repeat_ids_array, dtype=torch.long)

        coordinates = repeats_in_sequence[["start", "end"]]
        sample, coordinates = self.transform((sample, coordinates))

        target = {
            "seq_start": [start for _ in range(coordinates.shape[0])],
            "classes": repeat_ids_tensor,
            "coordinates": coordinates,
        }
        return (sample, target)

    def seq2seq(self, index):
        sequence, start, repeats_in_sequence = self.repeat_list[index]
        end = start + self.segment_length

        repeat_ids_series = repeats_in_sequence["subtype"].map(
            self.repeat_type_mapper.label_to_index
        )
        repeat_ids_array = np.array(repeat_ids_series, np.int32)
        # repeat_ids_tensor = torch.tensor(repeat_ids_array, dtype=torch.long)

        sample = {"sequence": sequence, "start": start}

        coordinates = repeats_in_sequence[["start", "end"]]
        sample, coordinates = self.transform((sample, coordinates))
        sample = sample["sequence"]
        target = sample.clone().detach()
        for coord, c in zip(coordinates, repeat_ids_array):
            start = int(coord[0].item())
            end = int(coord[1].item())
            repeat_cls = c.item() + self.dna_sequence_mapper.num_nucleobase_letters
            target[start:end] = repeat_cls
        # <sos> target <eos>
        sos = (
            self.repeat_type_mapper.num_categories
            + self.dna_sequence_mapper.num_nucleobase_letters
        )
        eos = sos + 1
        target = torch.cat(
            (
                torch.tensor([sos], dtype=torch.long),
                target,
                torch.tensor([eos], dtype=torch.long),
            )
        )
        return sample, target

    def __getitem__(self, index):
        return self.seq2seq(index)

    def __len__(self):
        return len(self.repeat_list)

    def collate_fn(self, batch):
        sequences = [data[0]["sequence"] for data in batch]
        seq_starts = [data[0]["start"] for data in batch]
        labels = [data[1] for data in batch]
        return torch.stack(sequences), seq_starts, labels


def build_dataloader(configuration):
    dna_sequence_mapper = DnaSequenceMapper()
    dataset = RepeatSequenceDataset(
        genome_fasta_path="./data/genome_assemblies/datasets",
        annotation_path="./data/annotations",
        chromosomes=configuration.chromosomes,
        segment_length=configuration.segment_length,
        overlap=configuration.overlap,
        dna_sequence_mapper=dna_sequence_mapper,
        transform=transforms.Compose(
            [
                SampleMapEncode(dna_sequence_mapper),
                CoordinatesToTensor(),
                NormalizeCoordinates(),
                TranslateCoordinates(),
            ]
        ),
    )
    configuration.num_classes = dataset.repeat_type_mapper.num_categories
    configuration.dna_sequence_mapper = dataset.dna_sequence_mapper
    configuration.num_nucleobase_letters = (
        configuration.dna_sequence_mapper.num_nucleobase_letters
    )
    dataset_size = len(dataset)
    if hasattr(configuration, "dataset_size"):
        dataset_size = min(dataset_size, configuration.dataset_size)
    indices = list(range(dataset_size))
    validation_size = int(configuration.validation_ratio * dataset_size)
    test_size = int(configuration.test_ratio * dataset_size)
    np.random.seed(configuration.seed)
    np.random.shuffle(indices)
    val_indices, test_indices, train_indices = (
        indices[:validation_size],
        indices[validation_size : validation_size + test_size],
        indices[validation_size + test_size : dataset_size],
    )

    train_sampler = SubsetRandomSampler(train_indices)
    valid_sampler = SubsetRandomSampler(val_indices)
    test_sampler = SubsetRandomSampler(test_indices)
    train_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=configuration.batch_size,
        sampler=train_sampler,
        collate_fn=dataset.collate_fn,
    )
    validation_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=configuration.batch_size,
        sampler=valid_sampler,
        collate_fn=dataset.collate_fn,
    )
    test_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=configuration.batch_size,
        sampler=test_sampler,
        collate_fn=dataset.collate_fn,
    )
    return train_loader, validation_loader, test_loader


class NormalizeCoordinates:
    """Normalize a sample's repeat annotation coordinates to a relative location
    in the sequence, defined as start and end floats between 0 and 1."""

    def __init__(self):
        pass

    def __call__(self, item):
        sample, coordinates = item
        length = len(sample["sequence"])
        start_coordinate = sample["start"]
        coordinates[:, :] -= start_coordinate
        coordinates[:, :] /= length
        return (sample, coordinates)


class TranslateCoordinates:
    """Convert (start, end) relative coordinates to (center, span)."""

    def __init__(self):
        pass

    def __call__(self, item):
        # [n, 2]
        sample, target = item
        center = (target[:, 1] + target[:, 0]) / 2  # [n]
        span = (target[:, 1]) - target[:, 0]  # [n]

        return (sample, torch.stack((center, span), axis=1))


if __name__ == "__main__":
    dna_sequence_mapper = DnaSequenceMapper()
    dataset = RepeatSequenceDataset(
        genome_fasta_path="./data/genome_assemblies/datasets",
        annotation_path="./data/annotations",
        chromosomes=["chrX"],
        dna_sequence_mapper=dna_sequence_mapper,
        transform=transforms.Compose(
            [
                SampleMapEncode(dna_sequence_mapper),
                CoordinatesToTensor(),
                NormalizeCoordinates(),
                TranslateCoordinates(),
            ]
        ),
    )

    print(dataset[0])
    # repeat_dict = dict()
    # for repeat in dataset:
    #     key = repeat[1]["classes"].nelement()
    #     repeat_dict[key] = repeat_dict.get(key, 0) + 1
    # print(repeat_dict)
    # index = 10100
    # index = 0
    # index = 165_970

    # import random

    # print(dataset[0])
    # while True:
    #     index = random.randint(1, 5000)
    #     item = dataset[index]
    #     print(f"{index=}, {item=}")
    #     annotation = item[1]
    #     if annotation["classes"].nelement() > 0:
    #         break
    # # dataloader, _ = build_dataloader()
    # for data in dataloader:
    #     print(data)
