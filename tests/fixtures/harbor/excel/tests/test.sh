#!/bin/sh
# Harbor task structure requires tests/test.sh. Bench-to-Work evaluates stages on the host
# controller via multi_round_harbor.verifier:ReportVerifier (configured automatically by scripts/run.sh).
echo 'Bench-to-Work evaluates tasks on the controller via multi_round_harbor.verifier:ReportVerifier (launch via scripts/run.sh).' >&2
exit 1
