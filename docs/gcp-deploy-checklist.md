# Google Cloud deployment: status record

What was planned, what was executed and what is still open. The commands and the findings are in `docs/deployment.md` section 3; this file is the checklist with an honest status per step.

| Step | What | Status |
|---|---|---|
| 0 | Application code: Gemini through the service account (`LLM_PROVIDER=vertex`), behind the existing `LLMClient` interface | done, tested offline and on the deployed system |
| 1 | `gcloud` installed and logged in (personal account) | done |
| 1b | An open billing account, a dedicated project, a budget alert | done (a new billing account was needed: the earlier ones were closed) |
| 2 | Enable APIs, create the Artifact Registry repository | done |
| 3 | Build the images with Cloud Build (amd64) from the committed files | done |
| 4 | Cloud SQL (PostgreSQL 17), database, user; `vector` extension created by the application at startup | done |
| 5 | Secrets in Secret Manager; a service account with least privilege | done |
| 6 | Deploy the API (service), the worker (worker pool), the frontend (service); knowledge ingestion as a job; first admin key through the Cloud SQL Auth Proxy | done |
| 7 | Verify end to end | done: webhook behaviours, a full investigation, all 36 scenarios through `scripts/eval_deployed.py`, the chat UI by hand |
| 8 | Update the documentation from "designed" to "deployed" | done (this change) |
| 9 | Self-hosted model on a Cloud Run GPU (vLLM, private, identity-token auth), weights from a project bucket | done and then deleted: measurements and what failed first are in `docs/deployment.md` section 3.8 (RTX PRO 6000 because the L4 had no quota; the smallest model could not complete the agent task, as expected) |
| 10 | Tear down and check the bill | open: the project is still running and billing |

Notes:

- The Google Cloud free-trial credit was not available (the account had used it), so everything is pay-as-you-go. Estimated running cost while deployed is a few dollars a day; deleting the project is the clean-up.
- Vertex AI is now called Gemini Enterprise Agent Platform in Google's documentation; the SDK parameter and our setting value are still `vertex`.
- Installing `gcloud` on macOS: the Homebrew cask failed with a certificate error inside the installer's bundled Python. Downloading the archive with curl and running `install.sh` with a system Python worked; the `worker-pools` commands additionally needed `grpcio` in that Python.
