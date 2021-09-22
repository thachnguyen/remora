from collections import Counter, defaultdict

import numpy as np
from taiyaki.mapped_signal_files import MappedSignalReader
import torch
import torch.nn.utils.rnn as rnn

from remora import constants, log, RemoraError
from remora.reference_functions import referenceEncoder

LOGGER = log.get_logger()

DEFAULT_BATCH_SIZE = 1024

# TODO convert module to be a batch generator


def get_motif_pos(ref, motif, motif_offset=0):
    return (
        np.where(
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
        + motif_offset
    )


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
    int_can_motif = np.array(
        [alphabet_info.alphabet.find(b) for b in can_motif]
    )

    return mod_base, int_can_motif, motif_offset


def load_chunks(
    dataset_path,
    num_chunks,
    mod_motif,
    chunk_context,
    context_bases,
    focus_offset=None,
    fixed_seq_len_chunks=False,
    base_pred=False,
    full=False,
):
    """
    Args:
        dataset_path: path to a hdf5 file generated by extract_toy_dataset
        num_chunks: size of returned dataset in number of instances
        mod_motif: modified base motif. mod_base, can_motif, mod_offset
        chunk_context: 2-tuple containing context signal or bases for each
            chunk
        context_bases: Number of bases before and after to included in encoded
            reference
        focus_offset: index of (mod)base in reference
        fixed_seq_len_chunks: return chunks with fixed sequence length and
            variable signal length. Default returns chunks with fixed signal
            length and variable length sequences.
        base_pred: Is this a base prediction model? Default: mods
        full: Is the input data a full read Taiyaki mapped signal file?
            Default: chunks

    Returns:
        sigs: list of signal chunks
        labels: list of mod/unmod labels for the corresponding chunks
        refs: list of reference sequences for each chunk
        base_locs: location for each base in the corersponing chunk
    """

    # TODO include a padding option
    read_data = MappedSignalReader(dataset_path)
    n_reads = len(read_data.get_read_ids())
    if num_chunks is None or num_chunks == 0 or num_chunks > n_reads:
        num_chunks = n_reads
    if not isinstance(num_chunks, int):
        raise ValueError("num_chunks must be an integer")
    if len(chunk_context) != 2:
        raise ValueError("chunk_context must be length 2")
    if any(not isinstance(cc, int) for cc in chunk_context):
        raise ValueError("chunk_context must be integers")
    if focus_offset is None and not full:
        raise ValueError("Either --focus-offset or --full need to be set.")

    # TODO allow multiple modified bases
    alphabet_info = read_data.get_alphabet_information()
    mod, int_can_motif, motif_offset = validate_motif(read_data, mod_motif)

    if base_pred and alphabet_info.alphabet != "ACGT":
        raise ValueError(
            "Base prediction is not compatible with modified base "
            "training data. It requires a canonical alphabet."
        )

    mod_idx = alphabet_info.alphabet.find(mod)

    ref_encoder = referenceEncoder(
        chunk_context, fixed_seq_len_chunks, context_bases
    )

    sigs = []
    labels = []
    refs = []
    enc_refs = []
    base_locations = []
    read_ids = []
    positions = []
    focus_offsets = []

    reject_reasons = defaultdict(int)
    for read in read_data:
        sig = read.get_current(read.get_mapped_dacs_region())
        # TODO actually grab correct region of reference
        # probably also 1-hot encode
        ref = "".join(
            alphabet_info.collapse_alphabet[b] for b in read.Reference
        )
        base_locs = read.Ref_to_signal - read.Ref_to_signal[0]
        if full:
            motif_pos = get_motif_pos(
                read.Reference, int_can_motif, motif_offset
            )
        else:
            motif_pos = [focus_offset]
        for m_pos in motif_pos:
            if fixed_seq_len_chunks:
                base_start = m_pos - chunk_context[0]
                base_end = m_pos + chunk_context[1] + 1
                if base_start <= 0:
                    reject_reasons["invalid_base_start"] += 1
                    continue
                if base_end >= len(base_locs):
                    reject_reasons["invalid_base_end"] += 1
                    continue
                sig_start = base_locs[base_start]
                sig_end = base_locs[base_end]
            else:
                # compute position at center of central base
                center_loc = (base_locs[m_pos] + base_locs[m_pos + 1]) // 2
                sig_start = center_loc - chunk_context[0]
                sig_end = center_loc + chunk_context[1]
                if sig_start < 0:
                    reject_reasons["invalid_signal_start"] += 1
                    continue
                if sig_end > base_locs[-1]:
                    reject_reasons["invalid_signal_end"] += 1
                    continue
                if len(sig[sig_start:sig_end]) == 0:
                    reject_reasons["empty signal"] += 1
                    continue

            enc_ref = ref_encoder.get_reference_encoding(
                sig_end - sig_start, ref, base_locs, m_pos
            )

            if base_pred:
                label = read.Reference[m_pos]
            else:
                label = int(read.Reference[m_pos] == mod_idx)
            labels.append(label)
            sigs.append(sig[sig_start:sig_end])
            refs.append(ref)
            enc_refs.append(enc_ref)
            base_locations.append(base_locs)
            read_ids.append(read.read_id)
            positions.append(read.Ref_to_signal[m_pos])
            focus_offsets.append(m_pos)

            reject_reasons["success"] += 1
            if reject_reasons["success"] >= num_chunks:
                break

    rej_summ = "\n".join(
        f"\t{count}\t{reason}"
        for count, reason in sorted(
            (count, reason) for reason, count in reject_reasons.items()
        )
    )
    LOGGER.info(f"Chunk selection summary:\n{rej_summ}\n")

    return (
        sigs,
        labels,
        refs,
        enc_refs,
        base_locations,
        read_ids,
        positions,
        focus_offsets,
    )


def collate_var_len_input(batch):
    """
    Pads batch of variable sequence lengths

    note: the output is passed to the pack_padded_sequence,
        so that variable sequence lenghts can be handled by
        the RNN
    """
    # get sequence lengths
    lens = torch.tensor([t[0].shape[0] for t in batch], dtype=np.long)
    mask = lens.ne(0)
    lens = lens[mask]

    # padding
    sigs = torch.nn.utils.rnn.pad_sequence(
        [torch.Tensor(t[0]).unsqueeze(1) for mt, t in zip(mask, batch) if mt]
    )
    sigs = rnn.pack_padded_sequence(sigs, lens, enforce_sorted=False)
    seqs = torch.nn.utils.rnn.pad_sequence(
        [torch.Tensor(t[1]).permute(1, 0) for mt, t in zip(mask, batch) if mt]
    )
    seqs = rnn.pack_padded_sequence(seqs, lens, enforce_sorted=False)
    # get labels
    if len(batch[0]) > 2:
        labels = torch.tensor(
            np.array([t[2] for mt, t in zip(mask, batch) if mt], dtype=np.long)
        )
        return (sigs, seqs, lens), labels
    else:
        return (sigs, seqs, lens)


def collate_fixed_len_input(batch):
    """Collate data with fixed width inputs

    Note that inputs with be in Time x Batch x Features (TBF) dimension order
    """
    # convert inputs to TBF
    sigs = torch.Tensor([t[0] for t in batch]).permute(1, 0).unsqueeze(2)
    seqs = torch.Tensor([t[1] for t in batch]).permute(2, 0, 1)
    if len(batch[0]) > 2:
        labels = torch.tensor(np.array([t[2] for t in batch], dtype=np.long))
        return (sigs, seqs), labels
    else:
        return (sigs, seqs)


class RemoraDataset(torch.utils.data.Dataset):
    def __init__(self, sigs, seqs, labels=None):
        self.sigs = sigs
        self.seqs = seqs
        self.use_labels = True if labels else False
        if self.use_labels:
            self.labels = labels

    def __getitem__(self, index):
        if self.use_labels:
            return self.sigs[index], self.seqs[index], self.labels[index]
        else:
            return self.sigs[index], self.seqs[index]

    def __len__(self):
        return len(self.sigs)


def load_datasets(
    dataset_path,
    chunk_context,
    focus_offset=None,
    batch_size=DEFAULT_BATCH_SIZE,
    num_chunks=None,
    fixed_seq_len_chunks=False,
    mod_motif=None,
    base_pred=True,
    val_prop=0.0,
    num_data_workers=0,
    context_bases=constants.DEFAULT_CONTEXT_BASES,
    infer=False,
    full=False,
):
    if not infer:
        (
            sigs,
            labels,
            refs,
            enc_refs,
            base_locs,
            read_ids,
            positions,
            focus_offsets,
        ) = load_chunks(
            dataset_path,
            num_chunks,
            mod_motif,
            chunk_context,
            context_bases,
            focus_offset,
            fixed_seq_len_chunks,
            base_pred,
            full,
        )
        label_counts = Counter(labels)
        if len(label_counts) <= 1:
            raise ValueError(
                "One or fewer output labels found. Ensure --focus-offset and "
                "--mod are specified correctly"
            )
        LOGGER.info(f"Label distribution: {label_counts}")

        tmp = list(
            zip(sigs, labels, refs, enc_refs, base_locs, read_ids, positions)
        )
        np.random.shuffle(tmp)
        sigs, labels, refs, enc_refs, base_locs, read_ids, positions = zip(*tmp)

        collate_fn = (
            collate_var_len_input
            if fixed_seq_len_chunks
            else collate_fixed_len_input
        )

        if val_prop <= 0.0:
            dl_val = dl_val_trn = None
            trn_set = RemoraDataset(sigs, enc_refs, labels)
        else:
            val_idx = int(len(sigs) * val_prop)
            val_set = RemoraDataset(
                sigs[:val_idx], enc_refs[:val_idx], labels[:val_idx]
            )
            val_trn_set = RemoraDataset(
                sigs[val_idx : val_idx + val_idx],
                enc_refs[val_idx : val_idx + val_idx],
                labels[val_idx : val_idx + val_idx],
            )
            trn_set = RemoraDataset(
                sigs[val_idx:], enc_refs[val_idx:], labels[val_idx:]
            )
            dl_val = torch.utils.data.DataLoader(
                val_set,
                batch_size=batch_size,
                shuffle=False,
                num_workers=num_data_workers,
                drop_last=False,
                collate_fn=collate_fn,
                pin_memory=True,
            )
            dl_val_trn = torch.utils.data.DataLoader(
                val_trn_set,
                batch_size=batch_size,
                shuffle=False,
                num_workers=num_data_workers,
                drop_last=False,
                collate_fn=collate_fn,
                pin_memory=True,
            )

        dl_trn = torch.utils.data.DataLoader(
            trn_set,
            batch_size=batch_size,
            shuffle=True,
            num_workers=num_data_workers,
            drop_last=True,
            collate_fn=collate_fn,
            pin_memory=True,
        )

        return dl_trn, dl_val, dl_val_trn
    else:
        (
            sigs,
            labels,
            refs,
            enc_refs,
            base_locations,
            read_ids,
            positions,
            focus_offsets,
        ) = load_chunks(
            dataset_path,
            num_chunks,
            mod_motif,
            chunk_context,
            context_bases,
            focus_offset,
            fixed_seq_len_chunks,
            base_pred,
            full,
        )

        collate_fn = (
            collate_var_len_input
            if fixed_seq_len_chunks
            else collate_fixed_len_input
        )
        test_set = RemoraDataset(sigs, enc_refs, labels)
        dl_test = torch.utils.data.DataLoader(
            test_set,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_data_workers,
            drop_last=False,
            collate_fn=collate_fn,
            pin_memory=True,
        )
        return dl_test, read_ids, positions
