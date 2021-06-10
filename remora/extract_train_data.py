from taiyaki.mapped_signal_files import MappedSignalReader
import pdb

def get_train_set(train_path, MOD_OFFSET,subset=[]):
    '''
    Args:
        train_path: path to a hdf5 file generated by extract_toy_dataset

    Returns:
        sigs: list of signal chunks
        labels: list of mod/unmod labels for the corresponding chunks
        refs: list of reference sequences for each chunk
        base_locs: location for each base in the corersponing chunk
    '''
    mod_training_msf = MappedSignalReader(train_path)
    alphabet_info = mod_training_msf.get_alphabet_information()
    sigs = []
    labels = []
    refs = []
    base_locs = []
    if len(subset) == 0:

    for read in mod_training_msf:
        sigs.append(read.get_current(read.get_mapped_dacs_region()))
        ref = "".join(alphabet_info.collapse_alphabet[b] for b in read.Reference)
        refs.append(ref)
        base_locs.append(read.Ref_to_signal - read.Ref_to_signal[0])
        is_mod = read.Reference[MOD_OFFSET] == 1
        labels.append(is_mod)
    return sigs, labels, refs, base_locs

    # TODO: write methods and robust API to train prediction model (for is_mod)
    # from sig and ref.
    # ref is fixed length (MOD_OFFSET * 2 + 1)
    # signal in this case is assigned by megalodon (so essentially the coarse
    # mapping from tombo2)
    # The exact mapping from reference bases to signal is found in base_locs.
    # base_locs need not be used in prediction, but may be used if desired.
