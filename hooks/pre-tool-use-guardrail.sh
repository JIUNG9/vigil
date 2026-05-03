#!/usr/bin/env bash
#
# teammate Claude Code PreToolUse hook — blocks dangerous tool calls.
#
# Wired into Claude Code's hook system (.claude/settings.json). Reads the
# tool invocation from stdin (JSON), decides allow/block, exits 0 (allow)
# or 2 (block — Claude Code surfaces the message to the user).
#
# What gets blocked (defense in depth):
#
#   1. `git push origin main`  / push to any protected branch
#   2. `terraform apply` against a prod-tagged directory
#   3. `kubectl apply -f` against a prod context
#   4. Edits to .github/workflows/*.yml on protected branches
#
# Override: set TEAMMATE_OVERRIDE=1 in shell env before launching Claude Code.

set -eu

# Read JSON payload from stdin. We use jq if available; otherwise fall back
# to a coarse grep-based parse (good enough for the patterns we care about).

payload=""
if [ -t 0 ]; then
  # No stdin data — Claude Code didn't pipe anything. Allow.
  exit 0
fi
payload=$(cat)

if [ -z "$payload" ]; then
  exit 0
fi

# --- Override fast path ---
if [ "${TEAMMATE_OVERRIDE:-0}" = "1" ]; then
  exit 0
fi

# Pull tool name + command-ish field. Different Claude Code versions wrap
# the payload differently; check the common shapes.
get_field() {
  field="$1"
  if command -v jq >/dev/null 2>&1; then
    echo "$payload" | jq -r --arg f "$field" '..|objects|.[$f]? // empty' 2>/dev/null | head -n1
  else
    echo "$payload" | grep -oE "\"$field\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n1 | sed -E "s/.*\"$field\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/"
  fi
}

tool_name=$(get_field "tool_name")
[ -z "$tool_name" ] && tool_name=$(get_field "name")
command_text=$(get_field "command")
[ -z "$command_text" ] && command_text=$(get_field "input")
file_path=$(get_field "file_path")

block_msg=""

# --- Bash-tool patterns ---
if echo "$tool_name" | grep -iqE '^bash$|run_command'; then
  case "$command_text" in
    *"git push origin main"*|*"git push origin master"*|\
    *"git push --force"*|*"git push -f "*|*"git push -f"$|*"git push origin +"*)
      block_msg="git push to a protected branch (or force push). Use a feature branch + PR."
      ;;
    *"terraform apply"*"prod"*|*"prod"*"terraform apply"*|*"terraform destroy"*)
      block_msg="terraform apply/destroy against a prod-tagged path. Use plan-only first, then a reviewed apply."
      ;;
    *"kubectl apply"*"prod"*|*"kubectl delete"*"prod"*|\
    *"kubectl rollout"*"prod"*)
      block_msg="kubectl mutation against a prod context. Run --dry-run=server first, or switch context."
      ;;
    *"rm -rf /"*|*"rm -rf ~"*|*"rm -rf $HOME"*)
      block_msg="rm -rf against /, ~, or \$HOME. This is almost always a typo."
      ;;
    *"DROP TABLE"*|*"DROP DATABASE"*|*"TRUNCATE TABLE"*)
      block_msg="destructive SQL (DROP/TRUNCATE). Use a migration with a review trail."
      ;;
  esac
fi

# --- File-edit patterns ---
if echo "$tool_name" | grep -iqE '^edit$|^write$|^multi_edit$' && [ -n "$file_path" ]; then
  case "$file_path" in
    *.github/workflows/*.yml|*.github/workflows/*.yaml)
      branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
      case "$branch" in
        main|master|production|prod|release)
          block_msg="editing $file_path directly on '$branch'. CI/CD changes need a PR with review."
          ;;
      esac
      ;;
  esac
fi

if [ -n "$block_msg" ]; then
  cat <<EOF >&2
teammate: BLOCKED — $block_msg

This is the teammate PreToolUse guardrail. Defense in depth, not a
replacement for branch protection or RBAC. Override:

  TEAMMATE_OVERRIDE=1 <re-launch your shell or Claude Code session>

EOF
  exit 2
fi

exit 0
