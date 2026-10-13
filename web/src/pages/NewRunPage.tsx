import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  useCreateRun,
  useDatasets,
  useDatasetVersions,
  useEvaluators,
  usePrompts,
  usePromptVersions,
  useRunChoices,
} from "../api/queries";
import type { Schemas } from "../api/client";
import { ErrorBox, Loading } from "../components/ui";
import { shortId } from "../lib/format";

/** "" means "latest": the API resolves a null version to the newest one. */
function toVersion(v: string): number | null {
  return v === "" ? null : Number(v);
}

export function NewRunPage() {
  const navigate = useNavigate();
  const datasets = useDatasets();
  const prompts = usePrompts();
  const evaluators = useEvaluators();
  const runs = useRunChoices();
  const create = useCreateRun();

  const [dataset, setDataset] = useState("");
  const [datasetVersion, setDatasetVersion] = useState("");
  const [prompt, setPrompt] = useState("");
  const [promptVersion, setPromptVersion] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [baseline, setBaseline] = useState("");
  const [ratio, setRatio] = useState("0.02");
  const [earlyStop, setEarlyStop] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const dsVersions = useDatasetVersions(dataset);
  const pVersions = usePromptVersions(prompt);

  const dsVersionNumber = toVersion(datasetVersion);
  const latestDs = datasets.data?.find((d) => d.name === dataset)?.latest_version ?? null;
  const effectiveDsVersion = dsVersionNumber ?? latestDs;
  const baselineChoices = (runs.data ?? []).filter(
    (r) =>
      r.dataset === dataset &&
      (effectiveDsVersion === null || r.dataset_version === effectiveDsVersion),
  );

  function toggleEvaluator(name: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!dataset) return setFormError("Choose a dataset.");
    if (!prompt) return setFormError("Choose a prompt.");
    if (picked.size === 0) return setFormError("Choose at least one evaluator.");
    const r = Number(ratio);
    if (!Number.isFinite(r) || r < 0 || r > 1) {
      return setFormError("Max failure ratio must be a number between 0 and 1.");
    }
    setFormError(null);
    const body: Schemas["RunIn"] = {
      dataset: { name: dataset, version: dsVersionNumber },
      prompt: { name: prompt, version: toVersion(promptVersion) },
      evaluators: [...picked].map((name) => ({ name, version: null })),
      baseline_run_id: baseline || null,
      max_failure_ratio: r,
      trigger: "manual",
      use_cache: true,
      early_stop: earlyStop,
    };
    create.mutate(body, { onSuccess: (run) => navigate(`/runs/${run.id}`) });
  }

  const loading = datasets.isPending || prompts.isPending || evaluators.isPending;
  const loadError = datasets.error ?? prompts.error ?? evaluators.error;

  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to="/">Runs</Link> <span aria-hidden="true">/</span> New run
      </nav>
      <div className="page-head">
        <h1>Start a run</h1>
      </div>
      {loading ? (
        <Loading label="Loading registry" />
      ) : loadError ? (
        <ErrorBox error={loadError} />
      ) : (
        <form className="panel form" onSubmit={submit} aria-label="Start a run">
          <div className="form-row">
            <label>
              <span>Dataset</span>
              <select
                value={dataset}
                onChange={(e) => {
                  setDataset(e.target.value);
                  setDatasetVersion("");
                  setBaseline("");
                }}
              >
                <option value="">Choose a dataset</option>
                {(datasets.data ?? []).map((d) => (
                  <option key={d.id} value={d.name}>
                    {d.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Dataset version</span>
              <select
                value={datasetVersion}
                disabled={!dataset}
                onChange={(e) => {
                  setDatasetVersion(e.target.value);
                  setBaseline("");
                }}
              >
                <option value="">Latest{latestDs ? ` (v${latestDs})` : ""}</option>
                {dsVersions.data?.map((v) => (
                  <option key={v.id} value={v.version}>
                    v{v.version} · {v.item_count} items
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="form-row">
            <label>
              <span>Prompt</span>
              <select
                value={prompt}
                onChange={(e) => {
                  setPrompt(e.target.value);
                  setPromptVersion("");
                }}
              >
                <option value="">Choose a prompt</option>
                {(prompts.data ?? []).map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Prompt version</span>
              <select
                value={promptVersion}
                disabled={!prompt}
                onChange={(e) => setPromptVersion(e.target.value)}
              >
                <option value="">Latest</option>
                {pVersions.data?.map((v) => (
                  <option key={v.id} value={v.version}>
                    v{v.version} · {v.model}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <fieldset>
            <legend>Evaluators</legend>
            {(evaluators.data ?? []).length === 0 ? (
              <p className="muted">No evaluators registered.</p>
            ) : (
              <div className="checks">
                {(evaluators.data ?? []).map((ev) => (
                  <label key={ev.id} className="check">
                    <input
                      type="checkbox"
                      checked={picked.has(ev.name)}
                      onChange={() => toggleEvaluator(ev.name)}
                    />
                    <span className="mono">{ev.name}</span>
                    <span className="muted small">{ev.kind.replace(/_/g, " ")}</span>
                  </label>
                ))}
              </div>
            )}
          </fieldset>

          <div className="form-row">
            <label>
              <span>Baseline run (optional)</span>
              <select
                value={baseline}
                onChange={(e) => setBaseline(e.target.value)}
                disabled={!dataset}
              >
                <option value="">None</option>
                {baselineChoices.map((r) => (
                  <option key={r.id} value={r.id}>
                    {shortId(r.id)} · {r.prompt}@{r.prompt_version} · {r.status}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Max failure ratio</span>
              <input
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={ratio}
                onChange={(e) => setRatio(e.target.value)}
              />
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={earlyStop}
                onChange={(e) => setEarlyStop(e.target.checked)}
              />
              <span>Stop early once the gates are decided (needs a finished baseline)</span>
            </label>
          </div>

          {(formError || create.isError) && (
            <div className="state state-error" role="alert">
              {formError ?? (create.error instanceof Error ? create.error.message : "Failed")}
            </div>
          )}
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={create.isPending}>
              {create.isPending ? "Starting…" : "Start run"}
            </button>
            <Link to="/" className="btn btn-quiet">
              Cancel
            </Link>
          </div>
        </form>
      )}
    </>
  );
}
