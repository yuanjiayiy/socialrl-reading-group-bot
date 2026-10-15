#!/usr/bin/env bash
# Creates a private GitHub repo for the reading-group bot, uploads the code,
# stores keys as encrypted secrets, and runs it once.
# Run inside the unzipped folder:  bash setup_github.sh [repo-name]
set -euo pipefail
REPO_NAME="${1:-reading-group-bot}"
cd "$(dirname "$0")"
say() { printf "\n\033[1m%s\033[0m\n" "$*"; }

for cmd in git gh; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Missing '$cmd'."
    [ "$cmd" = gh ] && echo "  macOS: brew install gh   Windows: winget install GitHub.cli   Other: https://cli.github.com"
    [ "$cmd" = git ] && echo "  https://git-scm.com/downloads"
    exit 1; }
done
gh auth status >/dev/null 2>&1 || { say "Log in to GitHub"; gh auth login --web --git-protocol https; }
OWNER="$(gh api user -q .login)"

say "Seed papers for the daily recommendation"
echo "Everything already in the voting and already-read tabs is used automatically."
echo "Paste any extra papers (arXiv links/IDs, DOIs, or titles), one per line; '-' prefix = less like this."
echo "Empty line when done."
SEEDS=""
while IFS= read -r line; do [ -z "$line" ] && break; SEEDS+="$line"$'\n'; done
[ -n "$SEEDS" ] && printf "%s" "$SEEDS" >> seeds.txt

say "Creating private repo $OWNER/$REPO_NAME"
[ -d .git ] || git init -q -b main
git add -A
git -c user.name="$OWNER" -c user.email="$OWNER@users.noreply.github.com" commit -q -m "Set up reading group bot" || true
if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  git push -u origin main
else
  gh repo create "$REPO_NAME" --private --source . --remote origin --push
fi
REPO="$OWNER/$REPO_NAME"
gh api -X PUT "repos/$REPO/actions/permissions/workflow" -f default_workflow_permissions=write >/dev/null 2>&1 || \
  echo "(Set Settings → Actions → General → Workflow permissions → Read and write, manually.)"

say "Secrets (typing is hidden; values go straight to GitHub, encrypted)"
echo "SLACK_BOT_TOKEN (xoxb-...)";            gh secret set SLACK_BOT_TOKEN -R "$REPO"
echo "SLACK_CHANNEL_ID (starts with C)";      gh secret set SLACK_CHANNEL_ID -R "$REPO"
echo "ANTHROPIC_API_KEY";                     gh secret set ANTHROPIC_API_KEY -R "$REPO"
while true; do
  read -r -p "Path to the Google service-account JSON key file: " KEYFILE
  KEYFILE="${KEYFILE/#\~/$HOME}"; KEYFILE="${KEYFILE%\"}"; KEYFILE="${KEYFILE#\"}"
  [ -f "$KEYFILE" ] && break; echo "Not found, try again."
done
gh secret set GOOGLE_SERVICE_ACCOUNT_JSON -R "$REPO" < "$KEYFILE"
EMAIL="$(grep -o '"client_email": *"[^"]*"' "$KEYFILE" | sed 's/.*"\([^"]*\)"$/\1/')"
read -r -p "Semantic Scholar API key? (y/N) " yn
[[ "$yn" =~ ^[Yy] ]] && gh secret set S2_API_KEY -R "$REPO"
read -r -p "Optional one-line group focus (Enter to skip): " focus
[ -n "$focus" ] && gh variable set GROUP_FOCUS -R "$REPO" --body "$focus"

say "Before the first run: share the spreadsheet with the bot"
echo "Open the sheet → Share → add  $EMAIL  as an Editor (untick 'Notify')."
read -r -p "Press Enter once that's done. "

say "Starting a test run (posts one recommendation now)"
sleep 3
gh workflow run paper-bot.yml -R "$REPO" -f mode=force && \
  echo "Watch it at https://github.com/$REPO/actions — a paper should show up in Slack in a few minutes." || \
  echo "Start it from https://github.com/$REPO/actions → paper-bot → Run workflow (mode: force)."
say "Done: https://github.com/$REPO"
