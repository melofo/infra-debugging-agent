# GCP deployment checklist

A step-by-step plan for deploying this project to Google Cloud: Cloud Run (API, frontend and worker pool), Cloud SQL for PostgreSQL with pgvector, Secret Manager, Artifact Registry and Gemini on Vertex AI. **Nothing beyond step 0 has been executed**: the deployment is paused at step 1 (see Status). Commands below are the intended plan; check flags with `--help` before running, because they were not run.

Why Vertex AI instead of the API key: free-trial credit cannot be spent on the AI Studio Gemini API, only on Vertex AI, and Vertex AI uses a service account identity (no key to store or leak). The application switches with `LLM_PROVIDER=vertex`, `GCP_PROJECT` and `GCP_LOCATION` (embeddings follow the same channel and keep the same index id, so an existing knowledge base stays valid).

## Status

| Step | What | Status |
|---|---|---|
| 0 | Application code: Vertex AI channel behind the existing `LLMClient` interface (12 offline tests, no GCP account needed) | **done** |
| 1 | `gcloud` installed and logged in | **done** |
| 1b | A billing account that is open, a dedicated project, a budget alert | **blocked**: both existing billing accounts are closed (see below) |
| 2 | Enable APIs, create the Artifact Registry repository | planned |
| 3 | Build the image (linux/amd64) with Cloud Build and push it | planned |
| 4 | Cloud SQL instance, database, user, `CREATE EXTENSION vector` | planned |
| 5 | Secrets in Secret Manager; service account with least privilege | planned |
| 6 | Deploy API (service), worker (worker pool), frontend (service) | planned |
| 7 | Verify end to end with a real investigation on Vertex AI | planned |
| 8 | Record evidence; change README and `docs/deployment.md` from "designed" to "deployed" | planned |
| 9 | Tear down and check the bill | planned |

## Where it stopped, and how to resume

- The only existing project is the one AI Studio created for the Gemini API key. Leave it alone and create a new project for the deployment, so tearing down cannot touch the key's project.
- `gcloud billing accounts list` shows both billing accounts with `OPEN = False`. The console says the account is closed and can be reopened from the account management page. Cost history is $0.00, so nothing was ever spent.
- To resume: reopen the billing account (it may need a credit card and upgrade to pay-as-you-go; the free trial is once per person and probably used), then continue at step 1b. Estimated cost for deploy, verify and tear down within a few days: about $3 to $8 (estimate, not measured).
- Local setup note (macOS): `brew install --cask google-cloud-sdk` failed with a certificate error in the installer's bundled Python. Downloading the archive with curl and running `install.sh` with `CLOUDSDK_PYTHON` pointing at a system Python 3.11 worked. Add `export CLOUDSDK_PYTHON=...` and `source ~/google-cloud-sdk/path.zsh.inc` to `~/.zshrc`, then `gcloud auth login` with a personal account.

## Steps

1. **Project and billing (you).** New project, link the billing account, create a budget alert of $20 (not the console default of $100). Confirm: `gcloud billing projects describe PROJECT_ID` shows billing enabled.
2. **APIs and registry.** Enable Cloud Run, Cloud SQL Admin, Artifact Registry, Secret Manager, Cloud Build and Vertex AI (`aiplatform.googleapis.com`); create a Docker repository in Artifact Registry.
3. **Image.** `gcloud builds submit` for the backend and the frontend (`NEXT_PUBLIC_API_URL` is a build argument). Cloud Build is used because an ARM Mac would otherwise produce an image Cloud Run cannot run.
4. **Database.** Smallest PostgreSQL instance; pgvector needs only `CREATE EXTENSION vector` by a user in `cloudsqlsuperuser`. Confirm the application user can run it, or create it once as the admin user. Reach it from Cloud Run through the Cloud SQL connection, from your laptop through the Cloud SQL Auth Proxy.
5. **Secrets and identity.** Database password, webhook secret and API-key admin credentials in Secret Manager. One service account with only: Vertex AI User, Cloud SQL Client, Secret Manager Secret Accessor.
6. **Deploy.** API as a Cloud Run service; worker as a Cloud Run worker pool (same image, `python scripts/run_worker.py`, no HTTP port, at least one instance); frontend as a Cloud Run service. Set `ENVIRONMENT=production`, `LLM_PROVIDER=vertex`, `GCP_PROJECT`, `GCP_LOCATION`.
7. **Verify.** Ingest the knowledge base through the proxy, send a simulated Jira event (the simulator may need a URL option), approve the proposed comment, and check the trace and the model's token usage. Save the output as evidence.
8. **Docs.** Change "designed" to "deployed" only after steps 2 to 7 actually ran.
9. **Tear down.** Delete the Cloud Run services and worker pool, the Cloud SQL instance, the secrets and the registry images; check the billing report the next day.
