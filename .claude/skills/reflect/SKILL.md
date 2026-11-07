---
name: reflect
description: Add an entry to PROCESS.md, the project's process log, from what the student says they did and learned plus every commit since the last entry, then open a pull request for it.
argument-hint: "[what you did and what you learned, in your own words]"
disable-model-invocation: true
---

# /reflect: a process log entry, from my words and my git history

You're helping me keep `PROCESS.md`, the process log for my final project.
The log is graded on whether it shows my design process as a loop: what I did, what I learned, and what I changed because of it.
My git history already records *what* changed; the log is where the *why* goes.

**My words are the source.
Git is the evidence.**
Never invent a person, an interview, a test, a quote, a result, or something I learned.
If it isn't in what I told you, it doesn't go in the entry.

## What changed since my last entry

Gathered when I typed `/reflect`:

```!
git fetch -q origin 2>/dev/null
echo "Today: $(date +%Y-%m-%d)"
echo "Branch: $(git status -sb | head -1)"
echo "Uncommitted:"; git status --short | head -20
last=$(git log -1 --format=%H -- PROCESS.md 2>/dev/null)
if [ -n "$last" ]; then
  echo "Last entry: $(git log -1 --date=short --format='%h %ad %s' "$last")"
  range="$last..HEAD"
else
  echo "Last entry: none yet (no commit has touched PROCESS.md)"
  range="HEAD"
fi
echo
echo "== Pull requests merged with a merge commit since then (squashed or rebased ones are in the commits below) =="
git log --merges --reverse --date=short --format='%h %ad %s' "$range" 2>/dev/null | grep -v 'reflect/' | head -30
echo
echo "== Commits since then, oldest first =="
git log --no-merges --reverse --date=short --format='--- %h %ad %s' --stat "$range" -- . ':(exclude)PROCESS.md' 2>/dev/null | head -250
echo
echo "== Changes to CLAUDE.md and .claude/ since then =="
if [ -n "$last" ]; then git diff "$range" -- CLAUDE.md .claude 2>/dev/null | head -80; else echo "(first entry: see the commits above)"; fi
echo
echo "== Images added since then =="
git log --diff-filter=A --name-only --format= "$range" -- '*.png' '*.jpg' '*.jpeg' '*.gif' '*.webp' 2>/dev/null | sort -u | head -30
echo
echo "== The end of PROCESS.md, for its format =="
tail -40 PROCESS.md 2>/dev/null || echo "(no PROCESS.md yet)"
true
```

## What I said

$ARGUMENTS

## What to do

1. **Hear me out.**
   If I didn't say anything above, ask me to tell you, in my own words, and mention that I can dictate instead of typing:
   - What did you work on, and which stage was it: Empathize, Define, Ideate, Prototype, Test, or building?
   - Who did you talk to or test with, and what did they do or say?
   - What did you learn, and what surprised you?
   - What did you change because of it?
   - What's next, and which assumption will it test?

   Then check what I told you against the commits.
   If something an entry needs is missing, ask me for it, one question at a time and three at most.
   The usual gaps: a test with no "what changed because of it", a change with no reason, or commits I haven't mentioned at all.
   If I don't know, or nothing changed, that's an honest answer: write it down as it is.

2. **Draft the entry**, in this shape, matching any entries already in `PROCESS.md`:

   ```markdown
   ## YYYY-MM-DD — a short title in my words

   **Stage:** Test (and any others)

   **What I did.** Two to four sentences: what I said, and what the commits show, at the level of what they mean rather than one line per commit.

   **What I learned.** From my words only.

   **What changed because of it.** The design, the code, or the plan, with the evidence.

   **Working with AI.** Only if there's something to say: a rule I added to CLAUDE.md and the mistake behind it, a place I took the keyboard back, a skill or hook I added.

   **Evidence.** Short commit hashes, PR numbers like #4, and images as relative links: ![paper prototype](process/paper-v2.jpg)

   **Next.** What I'll do next, and the assumption it tests.
   ```

   Keep my voice.
   First person, my phrasing, tidied only for dictation slips and typos.
   Plain and specific beats polished.

3. **Show me the draft, and wait.**
   Change it until I say it's right.

4. **Then open a pull request** for it:
   - If there are uncommitted changes, stop and ask what I want to do with them; never sweep them into this commit.
   - Start from an up-to-date `main`: `git switch main && git pull`.
   - Make a branch: `git switch -c reflect/YYYY-MM-DD` (add `-2` if it exists).
   - If there's no `PROCESS.md`, create it with a `# Process log` heading and one line saying what the project is.
   - Add the entry at the end, so the log reads oldest first.
   - Commit only `PROCESS.md`, plus any images I point you to: `git commit -m "Process log: <short title>"`.
   - `git push -u origin HEAD`.
   - Write the entry to a temporary file outside the repo, and open the PR from it, so quotes in the entry can't break the command: `gh pr create --base main --title "Process log: <short title>" --body-file <that file>`.
   - Give me the PR's URL, and remind me to read it and merge it.
     Don't merge it yourself.
