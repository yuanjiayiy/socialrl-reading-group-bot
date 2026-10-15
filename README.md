# Reading group paper bot

A Slack bot for the reading group that keeps the "Group meeting schedule - Social RL" sheet up to date.

1. **Shared papers go to the voting sheet.** Post a paper link in the channel and the bot adds a row to *Paper Voting*:
   - the title, linked, in the `[2506.15799] Title` style the sheet already uses
   - a description of at most 20 words, written by Claude from the abstract
   - who proposed it and today's date
   - unticked vote checkboxes for everyone, with the Total formula copied from the row above

   It replies in the thread to confirm, or to say the paper is already on the sheet or was already read.
2. **One recommendation a day**, with a 20-word summary. It learns from every paper on both tabs, plus `seeds.txt`, plus reactions:
   - 👍 means more like this
   - 👎 means less like this
   - ➕ adds the paper to the voting sheet
3. **Moving read papers.** `@paperbot read <part of title or link>` moves a paper from *Paper Voting* to the top of *Already read*. Add `presenter: Name` or `by @person` to record who presented it.
4. **Collapsing old rows.** Rows in *Paper Voting* whose Date Added is more than 4 weeks old are grouped and collapsed. The bot only re-collapses when the set of old rows changes, so expanding a group by hand will stick.

Type `@paperbot help` in the channel for the full command list.

## Setup

You need four things: a Slack app, a Google service account, an Anthropic API key, and the GitHub repo. The repo runs the bot every 30 minutes, for free.

### 1. Slack app
1. Go to https://api.slack.com/apps → *Create New App* → *From a manifest*.
2. Pick your workspace and paste `slack-app-manifest.yml` into the YAML tab. Click Create.
3. Go to *Install App* → *Install to Workspace*. Copy the `xoxb-…` token.
4. In the reading group channel, type `/invite @paperbot`.
5. Get the channel ID: click the channel name → *About* → it's at the bottom and starts with `C`.

### 2. Google service account
This is a robot Google account that the bot uses to edit the sheet.
1. Go to https://console.cloud.google.com, create a project (any name), and switch to it.
2. Open *APIs & Services* → *Library*, search **Google Sheets API**, and click Enable.
3. Open *IAM & Admin* → *Service Accounts* → *Create service account*. Give it any name and skip the optional steps.
4. Open the new account → *Keys* → *Add key* → *Create new key* → JSON. A file downloads. Keep it private.
5. Copy the account's email (`…@….iam.gserviceaccount.com`). In the spreadsheet, click *Share* and add that email as an **Editor**.

If your university's Google Workspace blocks sharing with outside accounts, the share in step 5 will fail. In that case, ask your Workspace admin to allow it. The other option is to make the copy of the sheet the bot edits a personal-account sheet.

### 3. GitHub
From inside this folder, run:

```
bash setup_github.sh
```

You need the GitHub CLI for this (`brew install gh`). The script then:
- creates a private repo and pushes the code
- asks for the Slack token, channel ID, Anthropic key and the JSON key file, and stores them as encrypted secrets
- reminds you to share the sheet
- posts a first recommendation as a test

## Settings

These are optional. Set them in the repo under *Settings → Secrets and variables → Actions → Variables*.

| Variable | Default | |
|---|---|---|
| `COLLAPSE_AFTER_DAYS` | `28` | Age at which rows get collapsed |
| `COLLAPSE_UNDATED` | `false` | Also collapse rows with an empty Date Added (most of the older rows) |
| `GROUP_FOCUS` | — | One line about the group's interests, used when picking the daily paper |
| `TIMEZONE` | `America/Los_Angeles` | For "date added" / "date read" |
| `SPREADSHEET_ID`, `VOTING_GID`, `READ_GID` | your sheet | If you ever move to a different sheet |

To change when the daily pick is posted, edit the first `cron` line in `.github/workflows/paper-bot.yml`. The `elif` line further down has to match it.

## Good to know

- The bot checks Slack every 30 minutes, so it isn't instant. Papers shared in thread replies aren't picked up; share them in the main channel.
- Columns are found by their header names. Adding a new member column after *Total*, or reordering columns, is fine. Renaming the *Paper*, *Description*, *Who proposed* or *Date Added* headers is not.
- The bot manages the row groups on the *Paper Voting* tab. If you add your own groups there, it will replace them.
- Some sites, OpenReview especially, block automated reads. When that happens the bot asks for the title: `@paperbot add <link> title: <title>`.
- A free Semantic Scholar API key (https://www.semanticscholar.org/product/api#api-key-form) makes the recommendations more reliable. The shared pool is often rate-limited.
- State (what's been recommended, reactions, lookups) lives in `state.json`, which the workflow commits back to the repo.
