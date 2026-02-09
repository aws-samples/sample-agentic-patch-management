# Project Intelligence

Context files for Kiro to maintain understanding across sessions.

## Files

| File | Purpose |
|------|---------|
| `project.md` | Why this exists, what it does, key design decisions, environment details |
| `architecture.md` | Agent system, routing, error handling, decision logger, infra stacks, data flow |
| `techstack.md` | Technologies, dependencies, deployment, observability, cost |
| `progress.md` | What's built, what's not, test results, git history, known limitations |

## Quick Reference

- **Deploy agent**: `./deploy.sh agent`
- **Chat**: `./chat.sh` (interactive, session-aware) or `./chat.sh -m "query"` (stateless)
- **AWS**: Profile `ri25-demo`, region `us-east-1`, account `125140434174`
- **S3 bucket**: `patch-compliance-reports-125140434174` (reports + decisions)
- **Status**: Pilot-ready, end-to-end tested (Feb 9, 2026)

**Last Updated**: February 9, 2026
