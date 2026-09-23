#!/usr/bin/env bash
# fake-headless-agent.sh — stand-in for headless-agent.sh in tests.
# Mirrors the real dispatcher's stdout contract (DISPATCH_ID/RUN_DIR/STREAM/FINAL/PID
# header lines, then "--- FINAL (exit N) ---" plus the answer when --wait is given)
# and the run-dir files (prompt.txt, meta.json, session_id, final.txt, exit_code).
# Behaviour is driven by markers inside the prompt:
#   @@REPLY:text@@                 -> final.txt = text
#   @@JSON:{...}@@                 -> final.txt = that JSON (wrapped in ``` fences if @@FENCE@@ present)
#   @@INVALID_THEN_VALID:{...}@@   -> invalid text on a fresh call, the JSON when called with --resume
#   @@FAIL_IF_MODEL:name@@         -> exit 1 with a 429 message when --model matches
#   @@SLEEP:seconds@@              -> sleep before answering
#   @@ECHO_PARENT@@                -> final.txt mentions parent=<session id> for fork/resume
#   @@NOSESSION@@                  -> no session_id file (a harness that could not report one)
#   @@FAIL_FIRST_FORK@@            -> the first fork of a given parent exits 1 with a 429
# Like real Claude Code, a claude_code --fork/--resume only finds a session from
# the directory it was created in ("No conversation found" otherwise).
# Every call appends one JSON line to $FAKE_RUN_ROOT/calls.log for assertions.
set -euo pipefail
ROOT="${FAKE_RUN_ROOT:-${TMPDIR:-/tmp}/fake-headless}"
mkdir -p "$ROOT"
harness=""; provider=""; model=""; effort=""; posture=""; dir=""; prompt=""; label=""; wait=0; fork=""; resume=""
openai_account=""
stream_format=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness) harness="$2"; shift 2;;
    --provider|--ai-provider) provider="$2"; shift 2;;
    --model) model="$2"; shift 2;;
    --openai-account) openai_account="$2"; shift 2;;
    --effort) effort="$2"; shift 2;;
    --format) stream_format="$2"; shift 2;;
    --posture) posture="$2"; shift 2;;
    --dir) dir="$2"; shift 2;;
    --add-dir) shift 2;;
    --context) shift 2;;
    --timeout) shift 2;;
    --prompt) prompt="$2"; shift 2;;
    --prompt-file) prompt="$(cat "$2")"; shift 2;;
    --label) label="$2"; shift 2;;
    --wait) wait=1; shift;;
    --fork|--fork-session) fork="$2"; shift 2;;
    --resume|--resume-session) resume="$2"; shift 2;;
    *) echo "fake-headless-agent: unknown flag $1" >&2; exit 2;;
  esac
done
# fork is only native on these harnesses, like the real router
if [[ -n "$fork" ]]; then
  case "$harness" in
    claude_code|codex|opencode|pi|prime-agent) ;;
    *) echo "headless-agent: --fork unsupported on harness $harness" >&2; exit 2;;
  esac
fi
# atomic counter (mkdir is the mutex; macOS has no flock)
until mkdir "$ROOT/.lock" 2>/dev/null; do sleep 0.01; done
n=$(( $(cat "$ROOT/counter" 2>/dev/null || echo 0) + 1 )); printf '%s' "$n" > "$ROOT/counter"; rmdir "$ROOT/.lock"
rd="$ROOT/run-$(printf '%04d' "$n")"; mkdir -p "$rd" "$ROOT/sessions"
printf '%s' "$prompt" > "$rd/prompt.txt"
if [[ -n "$fork" ]]; then sid="fork-of-$fork-$n"; elif [[ -n "$resume" ]]; then sid="$resume"; else sid="sess-$n"; fi
[[ "$prompt" == *"@@NOSESSION@@"* ]] || printf '%s\n' "$sid" > "$rd/session_id"
parent_sid="${fork:-$resume}"
if [[ -n "$parent_sid" && "$harness" == claude_code && -f "$ROOT/sessions/$parent_sid.dir" && "$(cat "$ROOT/sessions/$parent_sid.dir")" != "$dir" ]]; then
  printf 'No conversation found with session ID: %s' "$parent_sid" > "$rd/final.txt"; printf '1\n' > "$rd/exit_code"
  printf '{"n":%d,"harness":"%s","label":"%s","fork":"%s","resume":"%s","dir":"%s","code":1}\n' "$n" "$harness" "$label" "$fork" "$resume" "$dir" >> "$ROOT/calls.log"
  printf 'DISPATCH_ID : fake-%s\nRUN_DIR : %s\n' "$n" "$rd"
  exit 1
fi
printf '%s' "$dir" > "$ROOT/sessions/$sid.dir"
# session history: a resumed or forked session sees its parent's prompts, like a real harness
hist=""
if [[ -n "$fork" ]]; then hist="$(cat "$ROOT/sessions/$fork.prompt" 2>/dev/null || true)"; fi
if [[ -n "$resume" ]]; then hist="$(cat "$ROOT/sessions/$resume.prompt" 2>/dev/null || true)"; fi
printf '%s\n%s\n' "$hist" "$prompt" > "$ROOT/sessions/$sid.prompt"
prompt="$hist $prompt"
printf '{"tool":"fake","harness":"%s","provider":"%s","model":"%s","effort":"%s","posture":"%s","label":"%s"}\n' "$harness" "$provider" "$model" "$effort" "$posture" "$label" > "$rd/meta.json"
start=$(date +%s.%N)
if [[ "$prompt" =~ @@SLEEP:([0-9.]+)@@ ]]; then sleep "${BASH_REMATCH[1]}"; fi
code=0; out="OK: $label"
if [[ -n "$fork" && "$prompt" == *"@@FAIL_FIRST_FORK@@"* ]] && mkdir "$ROOT/failed-fork-$fork" 2>/dev/null; then
  out="API Error: Request rejected (429) rate limit"; code=1
elif [[ "$prompt" =~ @@FAIL_IF_MODEL:([^@]+)@@ ]] && [[ "$model" == "${BASH_REMATCH[1]}" ]]; then
  out="API Error: Request rejected (429) rate limit"; code=1
elif [[ "$prompt" =~ @@REPLY:([^@]*)@@ ]]; then out="${BASH_REMATCH[1]}"
elif [[ "$prompt" =~ @@JSON:(\{[^@]*\})@@ ]]; then
  out="${BASH_REMATCH[1]}"; [[ "$prompt" == *"@@FENCE@@"* ]] && out=$'Here you go:\n```json\n'"$out"$'\n```\nDone.'
elif [[ "$prompt" =~ @@INVALID_THEN_VALID:(\{[^@]*\})@@ ]]; then
  if [[ -n "$resume" ]]; then out="${BASH_REMATCH[1]}"; else out="Sorry, here is prose instead of JSON."; fi
fi
if [[ "$prompt" == *"@@ECHO_PARENT@@"* ]]; then out="$out parent=${fork:-${resume:-none}}"; fi
printf '%s' "$out" > "$rd/final.txt"; printf '%s\n' "$code" > "$rd/exit_code"
end=$(date +%s.%N)
printf '%s\n' "$openai_account" > "$rd/openai_account"
printf '%s\n' "$stream_format" > "$rd/format"
printf '{"n":%d,"harness":"%s","provider":"%s","model":"%s","effort":"%s","posture":"%s","label":"%s","fork":"%s","resume":"%s","start":%s,"end":%s,"code":%d}\n' "$n" "$harness" "$provider" "$model" "$effort" "$posture" "$label" "$fork" "$resume" "$start" "$end" "$code" >> "$ROOT/calls.log"
# dir goes in a sidecar log so existing calls.log consumers keep their shape
printf '{"n":%d,"dir":"%s"}\n' "$n" "$dir" >> "$ROOT/dirs.log"
printf 'DISPATCH_ID : fake-%s\n' "$n"
printf 'RUN_DIR : %s\nSTREAM  : %s/stream.jsonl\nFINAL   : %s/final.txt\nPID     : %s\n' "$rd" "$rd" "$rd" "$$"
if [[ "$wait" == 1 ]]; then printf -- '--- FINAL (exit %s) ---\n%s\n' "$code" "$out"; fi
exit "$code"
