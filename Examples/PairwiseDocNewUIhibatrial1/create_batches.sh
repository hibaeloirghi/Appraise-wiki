#!/bin/bash
set -x
python3 /Users/hibaeloirghi/Downloads/Appraise/Scripts/create_wmt22_pairwise_tasks.py -o batches -f /Users/hibaeloirghi/Downloads/Appraise/Examples/PairwiseDocNewUIhibatrial1/example.tsv --tsv \
    --max-segs 100 -s deu -t eng -A system-A -B system-B --rng-seed 1111 --no-qc 2>&1 | tee batches.run.log
