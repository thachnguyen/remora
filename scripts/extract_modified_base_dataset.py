import argparse
import atexit

import numpy as np
from tqdm import tqdm
from taiyaki.mapped_signal_files import MappedSignalReader, MappedSignalWriter

from remora import RemoraError


def get_motif_pos(ref, motif):
    return np.where(
        np.all(
            np.stack(
                [
                    motif[offset]
                    == ref[offset : ref.size - motif.size + offset + 1]
                    for offset in range(motif.size)
                ]
            ),
            axis=0,
        )
    )[0]


def extract_motif_dataset(
    input_msf, output_msf, mod_base, can_motif, motif_offset, context_bases
):
    alphabet_info = input_msf.get_alphabet_information()

    mod_motif = (
        can_motif[:motif_offset] + mod_base + can_motif[motif_offset + 1 :]
    )
    int_mod_base = alphabet_info.alphabet.find(mod_base)
    int_can_motif = np.array(
        [alphabet_info.alphabet.find(b) for b in can_motif]
    )
    int_mod_motif = np.array(
        [alphabet_info.alphabet.find(b) for b in mod_motif]
    )

    for read in tqdm(input_msf, smoothing=0):
        # select motif based on modified base content of read
        int_motif = (
            int_mod_motif
            if (read.Reference == int_mod_base).sum() > 0
            else int_can_motif
        )
        # select a random hit to the motif
        motif_hits = get_motif_pos(read.Reference, int_motif)
        motif_hits = motif_hits[
            np.logical_and(
                motif_hits > context_bases,
                motif_hits < read.Reference.size - context_bases - 1,
            )
        ]
        if motif_hits.size == 0:
            continue
        center_loc = np.random.choice(motif_hits, 1)[0] + motif_offset
        read_dict = read.get_read_dictionary()
        # trim signal and correct Ref_to_signal mapping
        ref_st = center_loc - context_bases
        ref_en = center_loc + context_bases + 1
        sig_st = read_dict["Ref_to_signal"][ref_st]
        sig_en = read_dict["Ref_to_signal"][ref_en]
        read_dict["Dacs"] = read_dict["Dacs"][sig_st:sig_en]
        read_dict["Ref_to_signal"] = read.Ref_to_signal[ref_st:ref_en] - sig_st
        read_dict["Reference"] = read.Reference[
            center_loc - context_bases : center_loc + context_bases + 1
        ]
        output_msf.write_read(read_dict)


def validate_motif(input_msf, motif):
    mod_base, can_motif, motif_offset = motif
    try:
        motif_offset = int(motif_offset)
    except ValueError:
        raise RemoraError(f'Motif offset not an integer: "{motif_offset}"')
    if motif_offset >= len(motif):
        raise RemoraError("Motif offset is past the end of the motif")
    alphabet_info = input_msf.get_alphabet_information()
    if mod_base not in alphabet_info.alphabet:
        raise RemoraError("Modified base provided not found in alphabet")
    if any(b not in alphabet_info.alphabet for b in can_motif):
        raise RemoraError(
            "Base(s) in motif provided not found in alphabet "
            f'"{set(can_motif).difference(alphabet_info.alphabet)}"'
        )
    can_base = can_motif[motif_offset]
    mod_can_equiv = alphabet_info.collapse_alphabet[
        alphabet_info.alphabet.find(mod_base)
    ]
    if can_base != mod_can_equiv:
        raise RemoraError(
            f"Canonical base within motif ({can_base}) does not match "
            f"canonical equivalent for modified base ({mod_can_equiv})"
        )

    return mod_base, can_motif, motif_offset


def get_parser():
    parser = argparse.ArgumentParser(
        description="Extract modified base model training dataset",
    )
    parser.add_argument(
        "mapped_signal_file",
        help="Taiyaki mapped signal file.",
    )
    parser.add_argument(
        "--output-mapped-signal-file",
        default="remora_modified_base_training_dataset.hdf5",
        help="Output Taiyaki mapped signal file. Default: %(default)s",
    )
    parser.add_argument(
        "--mod-motif",
        nargs=3,
        metavar=("BASE", "MOTIF", "REL_POSITION"),
        default=["m", "CG", 0],
        help="Extract training chunks centered on a defined motif. Argument "
        "takes 3 values representing 1) the single letter modified base(s), 2) "
        "sequence motif and 3) relative modified base position. For "
        'example to restrict to CpG sites use "--mod-motif m CG 0" (default).',
    )
    parser.add_argument(
        "--context-bases",
        type=int,
        default=50,
        help="Number of bases to either side of central base. "
        "Default: %(default)s",
    )

    return parser


def main(args):
    input_msf = MappedSignalReader(args.mapped_signal_file)
    atexit.register(input_msf.close)
    output_msf = MappedSignalWriter(
        args.output_mapped_signal_file, input_msf.get_alphabet_information()
    )
    atexit.register(output_msf.close)
    mod_base, can_motif, motif_offset = validate_motif(
        input_msf, args.mod_motif
    )
    extract_motif_dataset(
        input_msf,
        output_msf,
        mod_base,
        can_motif,
        motif_offset,
        args.context_bases,
    )


if __name__ == "__main__":
    main(get_parser().parse_args())