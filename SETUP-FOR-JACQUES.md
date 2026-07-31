# AI Video Studio — install guide

A local video editor that cuts your takes for you. You record yourself talking,
approve the script it writes, and it assembles the edit: picks the good attempt
of each line, throws out the false starts, removes the dead air, and syncs the
captions.

Everything runs on your Mac. Your footage never leaves it, and the AI edits run
on **your own** Claude or ChatGPT subscription, not on a shared server.

---

## Before you start

**You need a Mac** (Apple Silicon or Intel). About 5 GB of free disk, and
30–60 minutes, most of it downloads you don't have to watch.

**You need one AI subscription**, either Claude or ChatGPT. The app has no AI of
its own — it drives the CLI you are already logged into.

**One thing to understand before you install.** The app's chat is not a text
assistant: it runs shell commands on your Mac, with approvals turned off,
because that is how it cuts and renders video. It is the same power `claude`
has when you run it in a terminal yourself. That is why the app listens on
**your machine only** — nothing on your network can reach it. Install it on your
own machine, not on a shared or work-managed one.

---

## Install

### 1. Install the AI CLI and log in

Pick the one you have a subscription for. Claude is what this was built against.

```bash
npm i -g @anthropic-ai/claude-code
```

Then run `claude` once and complete the login in your browser.

If you are on ChatGPT instead:

```bash
npm i -g @openai/codex
```

Then run `codex` once and log in.

> If `npm` is not on your Mac yet, install Node first: `brew install node`.
> If you have no Homebrew either, skip this step — the installer in step 2 sets
> Homebrew and Node up for you. Come back and do this step afterwards.

### 2. Run the installer

```bash
git clone https://github.com/renatoklimeck/ai-video-studio.git ~/video-studio && cd ~/video-studio && ./install.sh
```

It asks for your password once (Homebrew) and asks one question: whether to
download the large transcription model (2.9 GB, noticeably better transcripts).
**Say yes** — that is what we use. If you skip it, say so, because the edits will
be measurably worse and we should not compare notes on a different model.

While it runs it installs ffmpeg, whisper, Node, uv and mkcert, builds the
interface, generates a locally-trusted HTTPS certificate, and registers a
background service so the app comes back after a reboot.

When it finishes it opens `https://localhost:3030` by itself.

### 3. Make the login durable

```bash
claude setup-token
```

Do this even if `claude` already works in your terminal. The normal login expires
when the app calls Claude in the background, and the failure looks like a broken
edit rather than an expired session. This command generates a credential that
lasts.

---

## Your first edit

1. **Import** your raw video: drag it onto the start screen, or click
   *Choose video…*
2. Click the preset **First edit — pick takes**.
3. It transcribes the video and writes a **clean script** — its reading of what
   you were trying to say, one line per sentence. A pop-up tells you when it is
   ready. Expect a few minutes here on a long video; the transcription is the
   slow part and it runs on your machine.
4. **Read the script and fix anything wrong**, then click *Approve script*. This
   is your control over the whole edit: from here on the cut follows those lines
   exactly.
5. It assembles the cut and generates the captions. Adjust anything you want on
   the timeline.
6. **Export**.

There is a second preset, **Clean pass — keep every good take**. Same script
step, but it does not choose between your good takes: if you recorded a line five
times and one was wrong, it removes the wrong one and the dead air and leaves the
other four side by side, labelled `take 2 of 4`, for you to pick from by hand.
Use it when you want to do the choosing yourself.

The chat takes plain requests: *"make the captions bigger"*, *"remove the silence
in take 3"*, *"use the other take for line 5"*.

---

## Updates

When Renato publishes a new version, an **↑ Update** button appears at the top of
the app. Click it and wait: it downloads, rebuilds, restarts and reloads the page
on its own. No terminal.

If you edited any file of the app yourself, the update sets your changes aside
and puts them back afterwards. If something fails, it rolls back to the version
that was working and tells you. Your projects are never touched by an update.

---

## If something goes wrong

**The app does not open.** Run `./install.sh` again from `~/video-studio`. It is
safe to re-run: it only does the steps that are missing, so it doubles as a
repair tool.

**The browser complains about the certificate.** Run `mkcert -install`, then
reload the page.

**The AI edits fail with an auth error.** Run `claude setup-token`.

**An edit stops halfway and the chat says the server restarted.** That is what it
says when the background service was restarted mid-edit (an update does this on
purpose). Nothing is damaged — send the request again.

**Anything else.** The server keeps a log at `~/video-studio/server.log`. Send
Renato the last 50 lines: `tail -50 ~/video-studio/server.log`

---

## Where things live

| path | what |
|---|---|
| `~/video-studio/projects/` | your videos and edits — **local only**, never uploaded, never in git |
| `~/loom-tools/models/` | transcription models (~3.4 GB, shared with other tools) |
| `~/.claude/video-studio-preferences.md` | the editing rules the AI follows — yours to edit, in the app |
| `~/video-studio/server.log` | server log |

To uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.aivideostudio.server.plist
```

then delete the `~/video-studio` folder.

---

## What "working" looks like

After step 3 you should be able to run these and get the same shape of answer:

```bash
curl -sk https://localhost:3030/api/engines
```

Expect `{"claude":true,...}` — if `claude` is `false`, the CLI is not installed or
not on the service's PATH, and no AI edit will run.

```bash
curl -sk https://localhost:3030/api/presets
```

Expect two presets: *First edit — pick takes* and *Clean pass — keep every good
take*. If you see zero, the app did not finish starting.
