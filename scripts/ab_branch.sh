#!/usr/bin/env bash
#
# Run an A/B test using two git branches, then evaluate the results.
#
# Usage:
#   scripts/ab_branch.sh \
#     --branch-a feature-x \
#     --branch-b feature-y \
#     --env-a .a.env \
#     --env-b .b.env \
#     [--eval-branch main] \
#     [--workspace my-workspace] \
#     -- "'Is the sky blue?' --budget 10"
#
set -euo pipefail

BRANCH_A=""
BRANCH_B=""
ENV_A=".env"
ENV_B=".env"
EVAL_BRANCH=""
WORKSPACE=""
COMMAND=""

usage() {
    cat <<'EOF'
Usage: scripts/ab_branch.sh [options] -- <main.py args>

Options:
  --branch-a BRANCH    Git branch for arm A (required)
  --branch-b BRANCH    Git branch for arm B (required)
  --env-a FILE         Env file for arm A (default: .env)
  --env-b FILE         Env file for arm B (default: .env)
  --eval-branch BRANCH Branch to run evaluation from (default: current branch)
  --workspace NAME     Workspace name (passed to main.py)
  -h, --help           Show this help

Everything after -- is passed to main.py (e.g. "'Question?' --budget 10").
The script automatically adds --staged and --run-id-file.
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch-a) BRANCH_A="$2"; shift 2 ;;
        --branch-b) BRANCH_B="$2"; shift 2 ;;
        --env-a)    ENV_A="$2"; shift 2 ;;
        --env-b)    ENV_B="$2"; shift 2 ;;
        --eval-branch) EVAL_BRANCH="$2"; shift 2 ;;
        --workspace) WORKSPACE="$2"; shift 2 ;;
        -h|--help)  usage ;;
        --)         shift; COMMAND="$*"; break ;;
        *)          echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$BRANCH_A" || -z "$BRANCH_B" ]]; then
    echo "Error: --branch-a and --branch-b are required"
    usage
fi
if [[ -z "$COMMAND" ]]; then
    echo "Error: main.py arguments required after --"
    usage
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
WT_A="/tmp/ab-wt-a-$$"
WT_B="/tmp/ab-wt-b-$$"
WT_EVAL=""
RUNID_FILE_A=$(mktemp)
RUNID_FILE_B=$(mktemp)
PID_A=""
PID_B=""

cleanup() {
    # Kill any running background processes
    if [[ -n "$PID_A" ]] && kill -0 "$PID_A" 2>/dev/null; then
        echo "Killing arm A (pid $PID_A)..."
        kill "$PID_A" 2>/dev/null || true
        wait "$PID_A" 2>/dev/null || true
    fi
    if [[ -n "$PID_B" ]] && kill -0 "$PID_B" 2>/dev/null; then
        echo "Killing arm B (pid $PID_B)..."
        kill "$PID_B" 2>/dev/null || true
        wait "$PID_B" 2>/dev/null || true
    fi
    # Remove worktrees
    if [[ -d "$WT_A" ]]; then
        git worktree remove --force "$WT_A" 2>/dev/null || true
    fi
    if [[ -d "$WT_B" ]]; then
        git worktree remove --force "$WT_B" 2>/dev/null || true
    fi
    if [[ -n "$WT_EVAL" && -d "$WT_EVAL" ]]; then
        git worktree remove --force "$WT_EVAL" 2>/dev/null || true
    fi
    rm -f "$RUNID_FILE_A" "$RUNID_FILE_B"
}
trap cleanup EXIT

# Verify branches exist
if ! git rev-parse --verify "$BRANCH_A" >/dev/null 2>&1; then
    echo "Error: branch '$BRANCH_A' does not exist"
    exit 1
fi
if ! git rev-parse --verify "$BRANCH_B" >/dev/null 2>&1; then
    echo "Error: branch '$BRANCH_B' does not exist"
    exit 1
fi

# Resolve env file paths to absolute before cd'ing into worktrees
ENV_A="$(cd "$REPO_ROOT" && realpath "$ENV_A")"
ENV_B="$(cd "$REPO_ROOT" && realpath "$ENV_B")"

echo "=== A/B Branch Test ==="
echo "Branch A: $BRANCH_A"
echo "Branch B: $BRANCH_B"
echo "Env A:    $ENV_A"
echo "Env B:    $ENV_B"
echo "Command:  $COMMAND"
echo ""

# Create worktrees
echo "Creating worktree for branch A ($BRANCH_A)..."
if ! git worktree add "$WT_A" "$BRANCH_A" 2>&1; then
    echo "Error: failed to create worktree for branch '$BRANCH_A'"
    exit 1
fi

echo "Creating worktree for branch B ($BRANCH_B)..."
if ! git worktree add "$WT_B" "$BRANCH_B" 2>&1; then
    echo "Error: failed to create worktree for branch '$BRANCH_B'"
    exit 1
fi

# Build workspace flag if provided
WS_FLAG=""
if [[ -n "$WORKSPACE" ]]; then
    WS_FLAG="--workspace $WORKSPACE"
fi

# Run both arms concurrently
echo ""
echo "Starting arm A..."
(cd "$WT_A" && uv run python main.py $COMMAND --staged --env-file "$ENV_A" --run-id-file "$RUNID_FILE_A" $WS_FLAG) &
PID_A=$!

echo "Starting arm B..."
(cd "$WT_B" && uv run python main.py $COMMAND --staged --env-file "$ENV_B" --run-id-file "$RUNID_FILE_B" $WS_FLAG) &
PID_B=$!

# Wait for both arms; if either fails, the trap will kill the other
echo "Waiting for both arms to complete..."
FAIL=""
wait "$PID_A" || FAIL="A"
PID_A=""
if [[ -n "$FAIL" ]]; then
    echo "Error: arm A failed"
    exit 1
fi

wait "$PID_B" || FAIL="B"
PID_B=""
if [[ -n "$FAIL" ]]; then
    echo "Error: arm B failed"
    exit 1
fi

# Read run IDs
RUN_ID_A=$(cat "$RUNID_FILE_A")
RUN_ID_B=$(cat "$RUNID_FILE_B")

if [[ -z "$RUN_ID_A" || -z "$RUN_ID_B" ]]; then
    echo "Error: failed to capture run IDs"
    echo "  Run ID A: '${RUN_ID_A:-<empty>}'"
    echo "  Run ID B: '${RUN_ID_B:-<empty>}'"
    exit 1
fi

echo ""
echo "=== Runs Complete ==="
echo "Run A: $RUN_ID_A (branch: $BRANCH_A)"
echo "Run B: $RUN_ID_B (branch: $BRANCH_B)"
echo ""

# Run evaluation
EVAL_CMD="uv run python main.py --ab-eval $RUN_ID_A $RUN_ID_B"
if [[ -n "$WORKSPACE" ]]; then
    EVAL_CMD="$EVAL_CMD --workspace $WORKSPACE"
fi

if [[ -n "$EVAL_BRANCH" ]]; then
    echo "Creating worktree for evaluation branch ($EVAL_BRANCH)..."
    WT_EVAL="/tmp/ab-wt-eval-$$"
    if ! git worktree add "$WT_EVAL" "$EVAL_BRANCH" 2>&1; then
        echo "Error: failed to create worktree for eval branch '$EVAL_BRANCH'"
        exit 1
    fi
    echo "Running evaluation from branch $EVAL_BRANCH..."
    (cd "$WT_EVAL" && $EVAL_CMD)
else
    echo "Running evaluation from current branch..."
    (cd "$REPO_ROOT" && $EVAL_CMD)
fi

echo ""
echo "=== A/B Branch Test Complete ==="
