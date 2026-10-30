---
title: Keeper
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: A student's inbox assistant on NVIDIA Nemotron 3
---

# Keeper, live

Keeper is a personal assistant for students. It sorts email, tracks internship and job applications, puts deadlines on the calendar and drafts replies for the student to send. This Space runs Keeper's real code from [the GitHub repository](https://github.com/SiddarthShekhar1994/nebius-x-nvidia). NVIDIA Nemotron 3 models on Nebius Token Factory make every call: Nano sorts each email and Super writes the replies.

Visitors can play the demo story (one internship, five emails: an application, an interview, a take-home deadline that moves, and a fake recruiter) or send Keeper an email of their own. Gmail and Google Calendar are in-memory stand-ins, so nobody connects an account and nothing is sent. The real Keeper runs as a Hermes agent in an NVIDIA OpenShell sandbox against a real inbox.

## Running this Space

The page is `space/` in the repository: `engine.py` drives Keeper's triage and `keeper act` steps, and `server.py` is a standard-library web server with a page in `static/`.

1. Create a Docker Space (blank template, free CPU, public).
2. Upload `space/Dockerfile` and `space/README.md` from the repository to the Space's root.
3. In the Space's settings, add a secret named `TOKENFACTORY_API_KEY`. Use a key made just for the demo, so you can revoke it on its own.

Optional variables: `KEEPER_DEMO_DAILY_CALLS` caps model calls per day across all visitors (default 600). Each visit is also capped at 40 calls, and the story needs at most 7.

Locally, from the repository root: `TOKENFACTORY_API_KEY=... python -m space.server`, then open http://localhost:7860.
