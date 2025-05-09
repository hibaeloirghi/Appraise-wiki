# pylint: disable=C0103,C0111,C0330,E1101
import argparse
import sys
from collections import OrderedDict
from collections import defaultdict
from copy import deepcopy
from glob import iglob
from json import dumps as json_dumps
from os.path import basename
from os.path import join
from random import choice
from random import randint
from random import seed
from random import shuffle
from typing import Any
from typing import Dict
from typing import List
from typing import Text
from typing import Tuple

from lxml import etree


MAX_TASK_SIZE = 100  # No support for tasks over 100 items
MAX_DOC_LENGTH = 100  # We do not support documents longer than 70 segments

MISSING_TRANSLATION_MESSAGE = ("NO TRANSLATION AVAILABLE",)
DEFAULT_TRANSLATOR = "DEFAULT"
# If False, documents with control items will be very last ones in each batch
SHUFFLE_DOCS_WITH_CONTROL_ITEMS = True
# If True, add references as additional system outputs
INCLUDE_REFERENCES_AS_SYSTEMS = True
# If True, documents may be oversampled to form the last batch
USE_ALL_DOCUMENTS_AND_ALL_SYSTEMS = True
REFERENCE_AS_SYSTEM_PREFIX = 'translator-'


def unwrap_xml(
    xml_file,
    missing_message=MISSING_TRANSLATION_MESSAGE,
    encoding='utf-8',
):
    """
    Unwraps an xml file in WMT format, producing source and (if present) reference files

    :param xml_file: The xml file (or fd)
    :param missing_message: The message to insert when no reference

    :returns: src_lang, src_lines, ref_lang, ref_lines, hyp_lang, hyp_lines

    ref_lines maps translator to document to tuples of segment id and line text
    hyp_lines maps system to document to tuples of segment id and line text

    ref_lang and hyp_lang may be None, and then their lines are empty
    note: a single language is assumed for each of sources, refs and hyps

    This function has been extracted from
    https://github.com/wmt-conference/wmt-format-tools/wmtformat/unwrap.py with
    some modifications
    """
    tree = etree.parse(xml_file)

    # Find and check  the documents (src, ref, hyp)
    src_langs, ref_langs, hyp_langs, translators, systems = (
        set(),
        set(),
        set(),
        set(),
        set(),
    )

    for src_doc in tree.getroot().findall(".//src"):
        src_langs.add(src_doc.get("lang"))

    for ref_doc in tree.getroot().findall(".//ref"):
        ref_langs.add(ref_doc.get("lang"))
        translator = ref_doc.get("translator")
        if translator:
            translators.add(translator)

    for hyp_doc in tree.getroot().findall(".//hyp"):
        hyp_langs.add(hyp_doc.get("lang"))
        systems.add(hyp_doc.get("system"))

    if len(src_langs) > 1:
        raise RuntimeError("Multiple source languages found")

    if len(src_langs) == 0:
        raise RuntimeError("No source languages found")

    src_lang = src_langs.pop()
    src_docs = OrderedDict()

    if len(ref_langs) > 1:
        raise RuntimeError("Multiple reference languages found")

    translators = list(translators)
    if len(ref_langs) > 0:
        if len(translators) == 0:
            print("No translator identifiers found")
            translators.append(DEFAULT_TRANSLATOR)
        ref_lang = ref_langs.pop()
        ref_docs = OrderedDict(
            (translator, OrderedDict()) for translator in translators
        )
    else:
        print("No references found")
        ref_lang = None
        ref_docs = OrderedDict()

    if len(hyp_langs) > 1:
        raise RuntimeError(f"Multiple hypothesis languages found: {hyp_langs}")

    systems = list(systems)
    if len(hyp_langs) > 0:
        hyp_docs = OrderedDict((system, OrderedDict()) for system in systems)
        hyp_lang = hyp_langs.pop()
    else:
        hyp_docs = OrderedDict()
        hyp_lang = None

    # Extract text
    src_sent_count, doc_count = 0, 0
    for doc in tree.getroot().findall(".//doc"):
        doc_id = doc.get("id")
        src = []
        if "testsuite" in doc.attrib:
            continue
        doc_count += 1
        src_sents = {int(seg.get("id")): seg.text for seg in doc.findall(".//src//seg")}

        def get_sents(doc):
            return {
                int(seg.get("id")): seg.text if seg.text else ""
                for seg in doc.findall(f".//seg")
            }

        if ref_lang:
            _ref_docs = doc.findall(".//ref")
            trans_to_ref = {}

            # If no translator identifiers, we just read one reference (if any)
            # If there are translator identifiers, we add a reference for each translator
            if len(translators) == 1 and DEFAULT_TRANSLATOR in translators:
                if len(_ref_docs):
                    trans_to_ref[DEFAULT_TRANSLATOR] = get_ref_sents(_ref_docs[0])
                else:
                    trans_to_ref[DEFAULT_TRANSLATOR] = {}
            else:
                trans_to_ref = {
                    ref_doc.get("translator"): get_sents(ref_doc)
                    for ref_doc in _ref_docs
                }

        if hyp_lang:
            _hyp_docs = doc.findall(".//hyp")
            system_to_ref = {
                hyp_doc.get("system"): get_sents(hyp_doc) for hyp_doc in _hyp_docs
            }

        for seg_id in sorted(src_sents.keys()):
            src.append([seg_id, src_sents[seg_id]])
            src_sent_count += 1
            if ref_lang:
                for translator in translators:
                    if doc_id not in ref_docs[translator]:
                        ref_docs[translator][doc_id] = []

                    # _ref_text = trans_to_ref.get(translator, {translator: {}}).get(
                    _ref_text = trans_to_ref[translator].get(seg_id, missing_message)
                    ref_docs[translator][doc_id].append((seg_id, _ref_text))

                    if _ref_text == MISSING_TRANSLATION_MESSAGE:
                        print(
                            f'Warning: missing reference for translator {translator}, '
                            f'document {doc_id}, segment {seg_id}'
                        )
            if hyp_lang:
                for system in systems:
                    if doc_id not in hyp_docs[system]:
                        hyp_docs[system][doc_id] = []

                    # _hyp_text = system_to_ref.get(system, {system: {}}).get(
                    _hyp_text = system_to_ref[system].get(seg_id, missing_message)
                    hyp_docs[system][doc_id].append((seg_id, _hyp_text))

                    if _hyp_text == MISSING_TRANSLATION_MESSAGE:
                        print(
                            f'Warning: missing translation from {system}, '
                            f'document {doc_id}, segment {seg_id}'
                        )

        src_docs[doc_id] = src

    print(
        f"Extracted {doc_count} document(s) containing {src_sent_count} sentences in {src_lang}"
    )

    return src_lang, src_docs, ref_lang, ref_docs, hyp_lang, hyp_docs


def unwrap_tsv(
    tsv_file,
    missing_message=MISSING_TRANSLATION_MESSAGE,
    encoding='utf-8',
    system_A='system-A',
    system_B='system-B',
):
    src_docs = OrderedDict()  # {doc_id: [(seg_id, src_text)}
    ref_docs = OrderedDict()  # {translator: {doc_id: [(seg_id, ref_text)]}}
    hyp_docs = OrderedDict()  # {system: {doc_id: [(seg_id, ref_text)]}}

    ref_docs['A'] = {}
    hyp_docs[system_A] = {}
    hyp_docs[system_B] = {}

    with open(tsv_file, "r", encoding=encoding) as tsv:
        for line in tsv:
            fields = line.rstrip("\n").split('\t')
            if len(fields) < 5:
                print(f"Error: too few fields in {tsv_file}, required fields: DocID, src, ref, sysA, sysB")
                exit()

            docid, src, ref, sysA, sysB = fields[:5]

            if docid not in src_docs:
                src_docs[docid] = []
            segid = len(src_docs[docid]) + 1  # segment ID is 1-based to keep it consistent with XML format
            src_docs[docid].append((segid, src))

            if docid not in ref_docs['A']:
                ref_docs['A'][docid] = []
            ref_docs['A'][docid].append((segid, ref))

            if docid not in hyp_docs[system_A]:
                hyp_docs[system_A][docid] = []
            hyp_docs[system_A][docid].append((segid, sysA))
            if docid not in hyp_docs[system_B]:
                hyp_docs[system_B][docid] = []
            hyp_docs[system_B][docid].append((segid, sysB))

    doc_count = len(src_docs)
    src_sent_count = sum(len(doc) for doc in src_docs.values())
    print(f"Extracted {doc_count} document(s) containing {src_sent_count} sentences")

    return src_docs, ref_docs, hyp_docs


def chop_docs(orig_src_docs, orig_ref_docs, orig_hyp_docs, max_length=10):
    """
    Split documents into chunks of max_length size.
    """
    src_docs = OrderedDict()
    src_prev = OrderedDict()
    src_next = OrderedDict()
    for doc_id, segs in orig_src_docs.items():
        for chunk, prev_ctx, next_ctx, chunk_id in _split_list(segs, max_length):
            src_docs[f"{doc_id}{chunk_id}"] = list(chunk)
            src_prev[f"{doc_id}{chunk_id}"] = list(prev_ctx)
            src_next[f"{doc_id}{chunk_id}"] = list(next_ctx)

    ref_docs = OrderedDict()
    hyp_prev = OrderedDict()
    hyp_next = OrderedDict()
    for translator in orig_ref_docs:
        ref_docs[translator] = OrderedDict()
        hyp_prev[REFERENCE_AS_SYSTEM_PREFIX + translator] = OrderedDict()
        hyp_next[REFERENCE_AS_SYSTEM_PREFIX + translator] = OrderedDict()
        for doc_id, segs in orig_ref_docs[translator].items():
            for chunk, prev_ctx, next_ctx, chunk_id in _split_list(segs, max_length):
                ref_docs[translator][f"{doc_id}{chunk_id}"] = list(chunk)
                hyp_prev[REFERENCE_AS_SYSTEM_PREFIX + translator][
                    f"{doc_id}{chunk_id}"
                ] = list(prev_ctx)
                hyp_next[REFERENCE_AS_SYSTEM_PREFIX + translator][
                    f"{doc_id}{chunk_id}"
                ] = list(next_ctx)

    hyp_docs = OrderedDict()
    for system in orig_hyp_docs:
        hyp_docs[system] = OrderedDict()
        hyp_prev[system] = OrderedDict()
        hyp_next[system] = OrderedDict()
        for doc_id, segs in orig_hyp_docs[system].items():
            for chunk, prev_ctx, next_ctx, chunk_id in _split_list(segs, max_length):
                hyp_docs[system][f"{doc_id}{chunk_id}"] = list(chunk)
                hyp_prev[system][f"{doc_id}{chunk_id}"] = list(prev_ctx)
                hyp_next[system][f"{doc_id}{chunk_id}"] = list(next_ctx)

    # print(src_prev)
    return src_docs, ref_docs, hyp_docs, src_prev, src_next, hyp_prev, hyp_next


def select_docs(orig_src_docs, orig_ref_docs, orig_hyp_docs, tsv_file):
    """
    Extract preselected segments from given documents and corresponding contexts.
    """
    selected_docs = []
    print("Selecting the following documents only:")
    with open(tsv_file, "r", encoding="utf8") as tsv:
        for line in tsv:
            _docid, _segid_first, _segid_last = line.strip().split("\t")
            selected_docs.append((_docid, int(_segid_first), int(_segid_last)))
            print(f"  {selected_docs[-1]}")

    src_docs = OrderedDict()
    src_prev = OrderedDict()
    src_next = OrderedDict()
    for doc_id, seg_id_1, seg_id_2 in selected_docs:
        if doc_id not in orig_src_docs:
            print(
                f"Error: the selected document {doc_id} not found in the XML file/src"
            )
            exit()
        segs = orig_src_docs[doc_id]
        chunk = segs[seg_id_1 - 1 : seg_id_2]
        prev_ctx = segs[0 : seg_id_1 - 1]
        next_ctx = segs[seg_id_2:]
        chunk_id = f"#{seg_id_1}-{seg_id_2}"

        src_docs[f"{doc_id}{chunk_id}"] = chunk
        src_prev[f"{doc_id}{chunk_id}"] = prev_ctx
        src_next[f"{doc_id}{chunk_id}"] = next_ctx

    ref_docs = OrderedDict()
    hyp_prev = OrderedDict()
    hyp_next = OrderedDict()
    for translator in orig_ref_docs:
        ref_docs[translator] = OrderedDict()
        hyp_prev[REFERENCE_AS_SYSTEM_PREFIX + translator] = OrderedDict()
        hyp_next[REFERENCE_AS_SYSTEM_PREFIX + translator] = OrderedDict()

        for doc_id, seg_id_1, seg_id_2 in selected_docs:
            if doc_id not in orig_ref_docs[translator]:
                print(
                    f"Error: the selected document {doc_id} not found in the XML file/ref"
                )
                exit()

            segs = orig_ref_docs[translator][doc_id]
            chunk = segs[seg_id_1 - 1 : seg_id_2]
            prev_ctx = segs[0 : seg_id_1 - 1]
            next_ctx = segs[seg_id_2:]
            chunk_id = f"#{seg_id_1}-{seg_id_2}"

            ref_docs[translator][f"{doc_id}{chunk_id}"] = chunk
            hyp_prev[REFERENCE_AS_SYSTEM_PREFIX + translator][
                f"{doc_id}{chunk_id}"
            ] = prev_ctx
            hyp_next[REFERENCE_AS_SYSTEM_PREFIX + translator][
                f"{doc_id}{chunk_id}"
            ] = next_ctx

    hyp_docs = OrderedDict()
    for system in orig_hyp_docs:
        hyp_docs[system] = OrderedDict()
        hyp_prev[system] = OrderedDict()
        hyp_next[system] = OrderedDict()

        for doc_id, seg_id_1, seg_id_2 in selected_docs:
            if doc_id not in orig_hyp_docs[system]:
                print(
                    f"Error: the selected document {doc_id} not found in the XML file/hyp"
                )
                exit()

            segs = orig_hyp_docs[system][doc_id]
            chunk = segs[seg_id_1 - 1 : seg_id_2]
            prev_ctx = segs[0 : seg_id_1 - 1]
            next_ctx = segs[seg_id_2:]
            chunk_id = f"#{seg_id_1}-{seg_id_2}"

            hyp_docs[system][f"{doc_id}{chunk_id}"] = chunk
            hyp_prev[system][f"{doc_id}{chunk_id}"] = prev_ctx
            hyp_next[system][f"{doc_id}{chunk_id}"] = next_ctx

    return src_docs, ref_docs, hyp_docs, src_prev, src_next, hyp_prev, hyp_next


def _split_list(list_a, chunk_size):
    for i in range(0, len(list_a), chunk_size):
        prev_context = list_a[0:i]
        next_context = list_a[i + chunk_size :]
        # 1-based to be consistent with other scripts and XML format
        chunk_id = f'#{i + 1}-{i + chunk_size + 1}'
        yield list_a[i : i + chunk_size], prev_context, next_context, chunk_id


def _create_bad_ref(seg_text: str, ref_text: str, character_based: bool = False) -> str:
    """
    Creates bad reference for given text.

    Segment length (a, b] to phrase length (excluding a, including b)
    mapping defined as follows:
        ( 0,   1] : 1
        ( 1,   5] : 2
        ( 5,   8] : 3
        ( 8,  15] : 4
        (15,  20] : 5
        (20, max] : 6

    For character-based languages, which do not support tokenisation
    by whitespace, the resulting phrase length will be doubled, and
    is interpreted as a character length.
    """
    seg_data = seg_text.split(' ')
    ref_data = ref_text.split(' ')

    if character_based:
        seg_data = [x for x in seg_text]
        ref_data = [x for x in ref_text]

    seg_len = len(seg_data)
    ref_len = len(ref_data)

    # Determine length of bad phrase, relative to segment length.
    _seg_to_bad_mapping = {
        (None, 1): 2,
        (1, 5): 2,
        (5, 8): 3,
        (8, 15): 4,
        (15, 20): 5,
        (20, None): 6,
    }

    bad_len = 0
    for seg_pair in _seg_to_bad_mapping:
        left, right = seg_pair

        # seg_len == right; left edge case
        if not left:
            if seg_len == right:
                bad_len = _seg_to_bad_mapping[seg_pair]
                break

        # left < seg_len; right edge case
        elif not right:
            if left < seg_len:
                bad_len = _seg_to_bad_mapping[seg_pair]
                break

        # left < seg_len <= right; middle cases
        elif left < seg_len <= right:
            bad_len = _seg_to_bad_mapping[seg_pair]
            break

    # Double length of bad phrase for character-based languages.
    if character_based:
        bad_len = 2 * bad_len

    # Determine random replacement position. For segments longer than
    # (bad_len + 1), we enforce that this cannot be sentence initial
    # or final, so positions 0 and (seg_len - bad_len -1) are invalid
    # and we use an embedded bad_pos in [1, (seg_len - bad_len - 1)].
    # This happens for all seg_len > 3.
    bad_pos = 0
    if seg_len - bad_len > 0:
        bad_pos = choice(range(seg_len - bad_len))

    elif seg_len > 3:
        _xs = max(1, seg_len - bad_len - 1)
        bad_pos = choice([x + 1 for x in range(_xs)])

    ref_pos = 0
    if ref_len - bad_len > 0:
        ref_pos = choice(range(ref_len - bad_len))

    bad_data = (
        seg_data[:bad_pos]
        + ref_data[ref_pos : ref_pos + bad_len]
        + seg_data[bad_pos + bad_len :]
    )
    bad_text = ' '.join(bad_data)
    if character_based:
        bad_text = ''.join(bad_data)

    # print(seg_text)
    # print(bad_text)
    # print('------------')
    return bad_text


def create_bad_refs(
    docs: Dict[str, List[Tuple[str, str]]],
    refs: Dict[str, List[Tuple[str, str]]],
    character_based: bool = False,
) -> Dict[str, List[Tuple[str, str]]]:
    """
    Creates bad references for given documents.

    For each segment in the given documents, this creates a so-called
    ``bad reference'' which is constructed by replacing an embedded
    phrase p with a randomly placed phrase p' of the same length,
    taken from a different segment contained in refs. The length of
    the phrase is relative to the full segment length.

    See _create_bad_ref() definition for length mapping details.
    """
    # Create mapping from f'{doc_id}_{seg_id}' to reference text.
    all_refs = {}
    for curr_doc_id, curr_doc in refs.items():
        for curr_seg_id, curr_ref_text in curr_doc:
            all_refs[f'{curr_doc_id}_{curr_seg_id}'] = curr_ref_text

    # Create list of f'{doc_id}_{seg_id}' ids, to be used for random
    # choice later when we want to identify a reference to work with.
    all_keys = list(all_refs.keys())

    # Iterate through documents and create bad references.
    bad_docs: Dict[str, List[Tuple[str, str]]] = OrderedDict()
    for curr_doc_id, curr_doc in docs.items():
        if not curr_doc_id in bad_docs:
            bad_docs[curr_doc_id] = []

        print(f'doc_id: {curr_doc_id},\tdoc_len: {len(curr_doc)}')
        for curr_seg in curr_doc:
            curr_seg_id, curr_seg_text = curr_seg

            # Bad reference id may not be identical to current id.
            bad_id = choice(all_keys)
            while bad_id == f'{curr_doc_id}_{curr_seg_id}':
                bad_id = choice(all_keys)

            curr_bad_text = _create_bad_ref(
                curr_seg_text,
                all_refs[bad_id],
                character_based=character_based,
            )

            # Ensure that keys can be reused.
            all_keys.append(bad_id)

            bad_docs[curr_doc_id].append((curr_seg_id, curr_bad_text))

    return bad_docs


def parse_cmd_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f",
        "--xml-file",
        help="path to .xml file with sources, references and system outputs",
        required=True,
    )
    parser.add_argument(
        "--tsv",
        help="-f is TSV file instead of XML, fields: docID, src, ref, sysA, sysB",
        action="store_true",
    )
    parser.add_argument(
        "-o",
        "--output-prefix",
        help="prefix for .csv and .json output files",
        required=True,
    )
    parser.add_argument(
        "-s",
        "--src-lang",
        help="ISO code for source language for Appraise",
        required=True,
    )
    parser.add_argument(
        "-t",
        "--tgt-lang",
        help="ISO code for target language for Appraise",
        required=True,
    )
    parser.add_argument(
        "-A",
        "--system-A",
        help="name of system A",
        required=True,
    )
    parser.add_argument(
        "-B",
        "--system-B",
        help="name of system B",
        required=True,
    )
    parser.add_argument(
        "-c",
        "--char-based",
        help="target language is character-based",
        action="store_true",
    )
    parser.add_argument(
        "--no-qc",
        help="do not generate BAD references as quality control items",
        action="store_true",
    )
    parser.add_argument(
        "--max-tasks",
        help="maximum number of tasks to generate, default: 100",
        type=int,
        default=100,
    )
    parser.add_argument(
        "--max-segs",
        help="maximum number of sentences per document",
        type=int,
        default=MAX_DOC_LENGTH,
    )
    parser.add_argument(
        "--rng-seed",
        help="seed for random number generator",
        type=int,
        default=123456,
    )
    parser.add_argument(
        "--selected-docs",
        help="path to a file with preselected documents; format: docid segid1 segid2",
    )
    parser.add_argument(
        "--static-context",
        help="number of preceding/succesive segments to show as a static context",
        type=int,
        default=MAX_DOC_LENGTH,  # a large number should use all available segments
    )
    parser.add_argument(
        "--even",
        help="duplicate one task if necessary to keep the total number of tasks even",
        action="store_true",
    )
    args = parser.parse_args()
    return (
        args.xml_file,
        args.output_prefix,
        args.src_lang,
        args.tgt_lang,
        args.char_based,
        not args.no_qc,
        args.max_tasks,
        args.max_segs,
        args.rng_seed,
        args.selected_docs,
        args.static_context,
        args.even,
        args.system_A,
        args.system_B,
        args.tsv,
    )


if __name__ == "__main__":
    """
    Example usage:
    python3 create_wmt22_pairwise_tasks.py -o batches.en-de -s enu -t deu -f newstest2021.en-de.all.xml -A system_name_A -B system_name_B
    python3 create_wmt22_pairwise_tasks.py -o batches.en-de -s enu -t deu -f docid-src-out1-out2.tsv --tsv
    """

    (
        XML_FILE,
        OUT_NAME,
        SRC_LANG,
        TGT_LANG,
        CHARLANG,
        CONTROLS,
        TASK_MAX,
        MAX_SEGS,
        RND_SEED,
        SELECTED,
        CTX_SIZE,
        EVEN_NUM,
        SYSTEM_A,
        SYSTEM_B,
        TSV_FILE,
    ) = parse_cmd_args()

    print(f'Character based={CHARLANG}')
    ENC = 'utf-8'
    seed(RND_SEED)

    print(f'Quality control={CONTROLS}')
    if not CONTROLS or TGT_LANG == 'sgg':  # no BAD refs if the target size has videos
        REQUIRED_SEGS = 50
    else:
        REQUIRED_SEGS = 80
    print(f'Setting REQUIRED_SEGS={REQUIRED_SEGS}')

    #################################################################
    SYS_DOCS: Dict[str, Dict[str, List[Tuple[str, str]]]] = OrderedDict()
    BAD_DOCS: Dict[str, Dict[str, List[Tuple[str, str]]]] = OrderedDict()
    print(f'Loading docs from {XML_FILE}')

    if TSV_FILE:
        SRC_DOCS, REF_DOCS, SYS_DOCS = unwrap_tsv(XML_FILE, encoding=ENC, system_A=SYSTEM_A, system_B=SYSTEM_B)
    else:
        src_lang, SRC_DOCS, ref_lang, REF_DOCS, hyp_lang, SYS_DOCS = unwrap_xml(
            XML_FILE, encoding=ENC
        )

    # Check if we have system A and system B in loaded docs
    for sys_name in [SYSTEM_A, SYSTEM_B]:
        if sys_name not in SYS_DOCS:
            print(f'Error: "{sys_name}" not found in loaded documents')
            exit()

    if SELECTED:
        docs_tuple = select_docs(SRC_DOCS, REF_DOCS, SYS_DOCS, SELECTED)
    else:
        docs_tuple = chop_docs(SRC_DOCS, REF_DOCS, SYS_DOCS, MAX_SEGS)

    (
        SRC_DOCS,
        REF_DOCS,
        SYS_DOCS,
        SRC_PREV,
        SRC_NEXT,
        SYS_PREV,
        SYS_NEXT,
    ) = docs_tuple

    # This reference will be used for generating BAD items
    REF_ID = sorted(list(REF_DOCS.keys()))[0]
    print(f'Using reference "{REF_ID}"')

    # Add references as additional system outputs
    if INCLUDE_REFERENCES_AS_SYSTEMS:
        for ref_id in sorted(list(REF_DOCS.keys())):
            sys_id = REFERENCE_AS_SYSTEM_PREFIX + ref_id
            print(f'Adding reference "{ref_id}" as system output "{sys_id}"')
            SYS_DOCS[sys_id] = REF_DOCS[ref_id]

    '''
    print(f"Keeping system A (= {SYSTEM_A}) and system B (= {SYSTEM_B}) only")
    for sys_id in list(SYS_DOCS.keys()):
        if sys_id not in [SYSTEM_A, SYSTEM_B]:
            print(f"  removing {sys_id}")
            del SYS_DOCS[sys_id]
    '''

    # hiba added this block
    print("Creating all possible system pairs for comparison")
    all_systems = sorted(list(SYS_DOCS.keys()))
    system_pairs = []
    for i in range(len(all_systems)):
        for j in range(i+1, len(all_systems)):
            system_pairs.append((all_systems[i], all_systems[j]))

    print(f"Generated {len(system_pairs)} unique system pairs")
    # end of block added

    # List of system names that can be iterated deterministically
    SYS_IDS = sorted(list(SYS_DOCS.keys()))
    print("SYS IDS size:", len(SYS_IDS))

    for sys_id in SYS_IDS:
        print(f'Generating bad references for {sys_id}')
        BAD_DOCS[sys_id] = create_bad_refs(
            SYS_DOCS[sys_id], REF_DOCS[REF_ID], character_based=CHARLANG
        )

    #################################################################
    # pylint: disable-msg=invalid-name
    some_sys_id = choice(SYS_IDS)
    some_doc_id = choice(sorted(list(SYS_DOCS[some_sys_id].keys())))
    some_sys_text = SYS_DOCS[some_sys_id][some_doc_id]
    some_bad_text = BAD_DOCS[some_sys_id][some_doc_id]
    print("Example:", some_sys_id, some_doc_id)

    for _s, _b in zip(some_sys_text, some_bad_text):
        print(_s)
        print(_b)
        print('---')

    #################################################################
    DOC_IDS = sorted(list(SYS_DOCS[some_sys_id].keys()))
    print("DOC IDS size:", len(DOC_IDS))

    # { doc_len : [( doc_len, doc_id, sys_id )] }
    DOC_STATS: Dict[int, List[Tuple[int, str]]] = OrderedDict()
    for doc_id in DOC_IDS:
        doc_len = len(SYS_DOCS[some_sys_id][doc_id])

        # We do not support documents longer than 70 segments.
        if doc_len > MAX_DOC_LENGTH:
            print("!!! DOCUMENT TOO LONG:", doc_id)
            continue

        if not doc_len in DOC_STATS.keys():
            DOC_STATS[doc_len] = []
        DOC_STATS[doc_len].append((doc_len, doc_id))

    # Randomise system order
    for doc_len in DOC_STATS:
        shuffle(DOC_STATS[doc_len])

    print("Doc. stats (doc.len/count):", DOC_STATS.keys())
    total_docs = 0
    total_sys = set(SYS_IDS)
    for doc_len in DOC_STATS.keys():
        print(f'  {doc_len}:\t{len(DOC_STATS[doc_len])}')
        total_docs += len(DOC_STATS[doc_len])
    print("total docs:", total_docs)
    print("total sys:", total_sys)

    #################################################################
    sampled_tasks: List[Tuple[Tuple[int, str], ...]] = []
    CURR_LEN = 0
    CURR_SYS = 0
    curr_task: List[Tuple[int, str]] = []
    DOC_STATS_COPY = deepcopy(DOC_STATS)
    last_task = False
    while DOC_STATS.keys():
        ALL_KEYS = sorted(list(DOC_STATS.keys()))
        # Maximum allowed length of a document to not exceed 100 segments in this task
        max_delta = REQUIRED_SEGS - CURR_LEN
        valid_keys = [x for x in ALL_KEYS if x <= max_delta]

        if not valid_keys:
            print("  #segments in current task:", CURR_LEN)
            for _doc in curr_task:
                print("   ", _doc)
            print('------')
            sampled_tasks.append(tuple(curr_task))
            CURR_LEN = 0
            curr_task = []
            if last_task:  # Stop if this was the last task with
                break
            continue

        # Take the document that fill in the allowed size perfectly, or random
        if max_delta in valid_keys:
            curr_key = max_delta
        else:
            curr_key = choice(valid_keys)

        CURR_LEN += curr_key
        curr_val = DOC_STATS[curr_key].pop(0)  # This takes a random system.

        curr_task.append(curr_val)
        if not DOC_STATS[curr_key]:
            DOC_STATS.pop(curr_key)

        # If there are some documents left that cannot form a full task with
        # 100 segments, take random documents to create the last task.
        # This ensures that all documents have been used at least once.
        if (
            USE_ALL_DOCUMENTS_AND_ALL_SYSTEMS
            and len(DOC_STATS) == 0
            and len(curr_task) > 0
        ):
            DOC_STATS = DOC_STATS_COPY
            last_task = True
            print('Creating last batch with padded documents')

    # Shuffle order of tasks
    shuffle(sampled_tasks)
    print("Total number of tasks:", len(sampled_tasks))

    #################################################################
    padded_tasks: List[Tuple[Tuple[int, str, bool], ...]] = []
    for tid, task in enumerate(sampled_tasks):
        task_docs = len(task)
        task_len = sum([x[0] for x in task])
        print(f'task_len: {task_len}')
        if task_len > MAX_TASK_SIZE:
            raise NotImplementedError(
                'No support for tasks >{0} items!'.format(MAX_TASK_SIZE)
            )

        elif task_len < MAX_TASK_SIZE:
            pad_size = MAX_TASK_SIZE - task_len
            pad_data: List[Tuple[int, str, bool]] = [(tup[0], tup[1], False) for tup in task]
            pad_pos = 0
            while pad_size > 0:
                print(f'pad_size: {pad_size}')
                print(f'pad_pos: {pad_pos}')
                pad_data.append((pad_data[pad_pos][0], pad_data[pad_pos][1], True))
                print(pad_data[-1])
                pad_size -= pad_data[-1][0]
                pad_pos = (pad_pos + 1) % task_docs
            if pad_size < 0:
                print(f'pad_size: {pad_size}')
                print(f'pad_pos: {pad_pos}')

                last_doc: Tuple[int, str, bool] = pad_data[-1]
                print('Making the last doc smaller', last_doc[0], '-->', last_doc[0] + pad_size)
                fixed_doc = (last_doc[0] + pad_size, *last_doc[1:])
                pad_data[-1] = fixed_doc
                # print(pad_data[-1][0])
            padded_tasks.append(tuple(pad_data))
            print("Padded tasks:")
            for _pad in padded_tasks[-1]:
                print("  ", _pad)

        else:
            print(f'WARNING: no control items in task no. {tid}')
            pad_data: List[Tuple[int, str, bool]] = [(tup[0], tup[1], False) for tup in task]
            padded_tasks.append(tuple(pad_data))

    if EVEN_NUM and len(padded_tasks) % 2 == 1:
        print('Duplicating one batch to keep the number of tasks even')
        padded_tasks.append(padded_tasks[0])
        print(f'Number of tasks now is {len(padded_tasks)}')

    #################################################################
    csv_data = []
    task_id = 0
    for task in padded_tasks:
        task_id += 1
        task_len = sum([tup[0] for tup in task])
        print(f'>>> task_len: {task_len}')

        for _doc in task:
            _data = [str(task_id)]
            for x in _doc:  # type: ignore
                _data.append(str(x))

            print('>>> ', ' '.join(_data))
            csv_data.append(','.join(_data))

    with open(f'{OUT_NAME}.csv', mode='w') as _file:
        for csv_line in csv_data:
            _file.write(csv_line)
            _file.write('\n')

    #################################################################
    json_data = []
    batch_id = 0
    _itemAll = 0
    for task in padded_tasks[:TASK_MAX]:
        # Remember, batch numbers are one-based
        task_data = OrderedDict(
            {
                'batchNo': batch_id + 1,
                'batchSize': 100,
                'sourceLanguage': SRC_LANG,
                'targetLanguage': TGT_LANG,
                'requiredAnnotations': 1,
                'randomSeed': RND_SEED,
            }
        )

        source_id = basename(XML_FILE)

        items_data: List[List[Dict[str, Any]]] = []  # Keeps items grouped into document
        _item = 0
        doc_counter = 0
        for doc_data in task:
            items_data.append([])  # Add a new bucket for items from this documents
            has_control_item = False
            print(doc_data)
            doc_len, doc_id, isControl = doc_data  # type: ignore

            # different order of systems per document only
            # hiba removed the following 2 lines
            #_shuffled_sys_ids = SYS_IDS.copy()
            #shuffle(_shuffled_sys_ids)

            # and added these 2 lines
            for sys_A, sys_B in system_pairs:
                _shuffled_sys_ids = [sys_A, sys_B]

                _src = {}
                _ref = {}
                _bads = {}
                _tgts = {}

                for item_id, item_src in SRC_DOCS[doc_id]:
                    seg_id = f'{doc_id}::{item_id}'
                    _src[seg_id] = item_src

                for item_id, item_ref in REF_DOCS[REF_ID][doc_id]:
                    seg_id = f'{doc_id}::{item_id}'
                    _ref[seg_id] = item_ref

                for sys_id in SYS_IDS:
                    _bads[sys_id] = {}
                    _tgts[sys_id] = {}

                    for item_id, item_bad in BAD_DOCS[sys_id][doc_id]:
                        seg_id = f'{doc_id}::{item_id}'
                        _bads[sys_id][seg_id] = item_bad

                    for item_id, item_tgt in SYS_DOCS[sys_id][doc_id]:
                        seg_id = f'{doc_id}::{item_id}'
                        _tgts[sys_id][seg_id] = item_tgt

                seg_counter = 0
                context_src: List[Text] = []
                context_ref: List[Text] = []
                context_bads: Dict[str, List[Text]] = defaultdict(list)
                context_tgts: Dict[str, List[Text]] = defaultdict(list)
                for seg_id in _src:
                    if seg_counter >= doc_len:  # Padding tasks are shorter!
                        break
                    item_src = _src[seg_id]
                    item_ref = _ref[seg_id]

                    item_bads = { sys_id: _bads[sys_id][seg_id] for sys_id in SYS_IDS }
                    item_tgts = { sys_id: _tgts[sys_id][seg_id] for sys_id in SYS_IDS }
                    item_type = 'TGT'

                    # Do not generate any BAD items if QC is disabled
                    if CONTROLS and isControl:
                        randomCoinFlip = choice(
                            [False, False, True, True, True]  # 60:40 chance
                        )
                        if randomCoinFlip:
                            item_tgts = item_bads
                            item_type = 'BAD'
                            has_control_item = True

                    src_ctx = []
                    if seg_counter == 0:
                        src_ctx = [txt for _, txt in SRC_PREV[doc_id]][-CTX_SIZE:]

                    obj: Dict[str, Any] = OrderedDict()
                    obj['_item'] = _item
                    obj['_block'] = -1
                    obj['segmentID'] = f'{source_id}::{seg_id}'
                    obj['segmentContextLeft'] = '\n'.join(src_ctx)
                    obj['segmentText'] = item_src
                    obj['itemID'] = seg_counter
                    obj['itemType'] = item_type
                    obj['documentID'] = doc_id
                    obj['isCompleteDocument'] = False
                    obj['targets'] = []
                    obj['targetsSize'] = 0

                    for tgt_idx, sys_id in enumerate(_shuffled_sys_ids):
                        tgt_ctx = []
                        if seg_counter == 0:
                            tgt_ctx = [txt for _, txt in SYS_PREV[sys_id][doc_id]][-CTX_SIZE:]

                        tobj = OrderedDict()
                        tobj['_itemAll'] = _itemAll
                        tobj['_target'] = tgt_idx
                        tobj['targetID'] = sys_id
                        tobj['targetContextLeft'] = '\n'.join(tgt_ctx)
                        tobj['targetText'] = item_tgts[sys_id]

                        obj['targets'].append(tobj)
                        obj['targetsSize'] += 1
                        _itemAll += 1

                    context_src.append(item_src)
                    context_ref.append(item_ref)
                    for sys_id in SYS_IDS:
                        context_bads[sys_id].append(item_bads[sys_id])
                        context_tgts[sys_id].append(item_tgts[sys_id])

                    items_data[-1].append(obj)
                    _item += 1
                    seg_counter += 1

                src_ctx = []
                src_ctx = [txt for _, txt in SRC_NEXT[doc_id]][:CTX_SIZE]

                obj = OrderedDict()
                obj['_item'] = _item
                obj['_block'] = -1
                obj['segmentID'] = f'{source_id}::{seg_id}'
                obj['segmentContextLeft'] = '\n'.join(src_ctx)
                obj['segmentText'] = ' '.join(context_src)  # full document
                obj['itemID'] = item_id
                obj['itemType'] = 'BAD' if has_control_item else 'TGT'
                obj['documentID'] = doc_id
                obj['isCompleteDocument'] = True
                obj['targets'] = []
                obj['targetsSize'] = 0

                for tgt_idx, sys_id in enumerate(_shuffled_sys_ids):
                    tgt_ctx = []
                    tgt_ctx = [txt for _, txt in SYS_NEXT[sys_id][doc_id]][:CTX_SIZE]

                    tobj = OrderedDict()
                    tobj['_itemAll'] = _itemAll
                    tobj['_target'] = tgt_idx
                    tobj['targetContextLeft'] = '\n'.join(tgt_ctx)
                    tobj['targetID'] = sys_id
                    tobj['targetText'] = ' '.join(context_tgts[sys_id])  # full document

                    obj['targets'].append(tobj)
                    obj['targetsSize'] += 1
                    _itemAll += 1

                items_data[-1].append(obj)

                if has_control_item and SHUFFLE_DOCS_WITH_CONTROL_ITEMS:
                    # Move the document with control items to a random position so
                    # that they are not accumulated as very last documents
                    _bad_doc = items_data.pop()
                    _pos = randint(0, len(items_data) - 1)
                    print(f'  Moving the last QC document to position {_pos}')
                    items_data.insert(_pos, _bad_doc)

        # Extract items from documents
        _items_data = [item for doc_items in items_data for item in doc_items]
        # Re-assign _item numbers
        if SHUFFLE_DOCS_WITH_CONTROL_ITEMS:
            _item = 0
            for i in range(len(_items_data)):
                _items_data[i]['_item'] = _item
                if _items_data[i]['isCompleteDocument'] == False:
                    _item += 1

        output_data = OrderedDict({'task': task_data, 'items': _items_data})

        json_data.append(output_data)

        # write out JSON
        json_text = json_dumps(json_data, indent=2, sort_keys=True)

        json_file_name = f'{OUT_NAME}.json'
        with open(json_file_name, mode='w', encoding='utf8') as out_file:
            sys.stdout.write(
                'Creating {0}, batch no. {1} ... '.format(json_file_name, batch_id + 1),
            )
            out_file.write(str(json_text))
            sys.stdout.write('OK\n')

        batch_id += 1

    print(f'Total tasks: {len(sampled_tasks)}')
    print(f'Total docs:  {total_docs} x {len(SYS_IDS)}')
    print(f'Total sys:   {len(total_sys)} {sorted(list(total_sys))}')
